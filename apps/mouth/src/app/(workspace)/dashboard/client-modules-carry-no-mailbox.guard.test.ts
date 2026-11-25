import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const PLATFORM_SENDER = "zantara@balizero.com";
const dashboardDirectory = path.resolve(
  process.cwd(),
  "src/app/(workspace)/dashboard",
);
const repositoryDirectory = path.resolve(process.cwd(), "../..");

function source(text: string, fileName = "synthetic.tsx") {
  return ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

function syntaxErrors(text: string, fileName: string): string[] {
  const result = ts.transpileModule(text, {
    fileName,
    reportDiagnostics: true,
    compilerOptions: { jsx: ts.JsxEmit.Preserve },
  });
  return (result.diagnostics ?? [])
    .filter((d) => d.category === ts.DiagnosticCategory.Error)
    .map((d) => {
      const line =
        ts.getLineAndCharacterOfPosition(source(text, fileName), d.start ?? 0)
          .line + 1;
      return `${fileName}:${line}: ${ts.flattenDiagnosticMessageText(d.messageText, " ")}`;
    });
}

function isClientModule(sourceFile: ts.SourceFile): boolean {
  for (const statement of sourceFile.statements) {
    if (
      !ts.isExpressionStatement(statement) ||
      !ts.isStringLiteral(statement.expression)
    )
      return false;
    const raw = statement.expression.getText(sourceFile);
    if (raw === '"use client"' || raw === "'use client'") return true;
  }
  return false;
}

function decodeJsxEntities(text: string): string {
  const named: Record<string, string> = {
    amp: "&",
    apos: "'",
    commat: "@",
    lowbar: "_",
    period: ".",
    percnt: "%",
    plus: "+",
    quot: '"',
  };
  return text.replace(
    /&#(x[0-9a-f]+|[0-9]+);|&([a-z]+);/gi,
    (match, numeric, name) => {
      if (numeric)
        return String.fromCodePoint(
          numeric[0].toLowerCase() === "x"
            ? Number.parseInt(numeric.slice(1), 16)
            : Number.parseInt(numeric, 10),
        );
      return named[name.toLowerCase()] ?? match;
    },
  );
}

function stringEntries(
  sourceFile: ts.SourceFile,
): { value: string; line: number }[] {
  const values: { value: string; line: number }[] = [];
  const push = (node: ts.Node, value: string) =>
    values.push({
      value,
      line:
        sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile))
          .line + 1,
    });
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node))
      push(node, decodeJsxEntities(node.getText(sourceFile)));
    else if (
      ts.isStringLiteral(node) &&
      ts.isJsxAttribute(node.parent) &&
      node.parent.initializer === node
    ) {
      const raw = node.getText(sourceFile);
      push(node, decodeJsxEntities(raw.slice(1, -1)));
    } else if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node)
    )
      push(node, node.text);
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return values;
}

function stringValues(sourceFile: ts.SourceFile): string[] {
  return stringEntries(sourceFile).map(({ value }) => value);
}

const atext = "A-Za-z0-9!#$%&'*+/=?^_`{|}~.\\-";
const localCharacter = new RegExp(`[${atext}]`);
function quotedLocalBefore(value: string, at: number): string | undefined {
  for (let start = 0; start < at; start += 1) {
    if (value[start] !== '"') continue;
    let cursor = start + 1;
    while (cursor < at && value[cursor] !== '"')
      cursor += value[cursor] === "\\" ? 2 : 1;
    if (cursor === at - 1 && value[cursor] === '"')
      return value.slice(start, at);
  }
  return undefined;
}

function findMailboxes(value: string): string[] {
  const found: string[] = [];
  for (let at = 0; at < value.length; at += 1) {
    if (value[at] !== "@") continue;
    const quoted = quotedLocalBefore(value, at);
    let local = quoted ?? "";
    if (!quoted) {
      let start = at;
      while (start > 0 && localCharacter.test(value[start - 1])) start -= 1;
      local = value.slice(start, at);
    }
    if (!local) continue;
    let cursor = at + 1;
    const labels: string[] = [];
    while (cursor < value.length) {
      const label = value.slice(cursor).match(/^[A-Za-z0-9-]+/)?.[0];
      if (!label) break;
      labels.push(label);
      cursor += label.length;
      if (
        value[cursor] !== "." ||
        !/[A-Za-z0-9-]/.test(value[cursor + 1] ?? "")
      )
        break;
      cursor += 1;
    }
    if (
      labels.length >= 2 &&
      /^[A-Za-z]{2,}$/.test(labels[labels.length - 1] ?? "")
    )
      found.push(`${local}@${labels.join(".")}`);
  }
  return found;
}

function isAllowedMailbox(mailbox: string): boolean {
  if (mailbox.toLowerCase() === PLATFORM_SENDER.toLowerCase()) return true;
  const domain = mailbox.slice(mailbox.lastIndexOf("@") + 1).toLowerCase();
  return [
    "example.com",
    "example.net",
    "example.org",
    "example",
    "test",
    "invalid",
    "localhost",
  ].some((reserved) => domain === reserved || domain.endsWith(`.${reserved}`));
}

function checkSenderDrift(pythonText: string): boolean {
  const canonical = `LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"`;
  const lines = pythonText.split(/\r?\n/);
  if (lines.filter((line) => line.trimEnd() === canonical).length !== 1)
    return false;
  return lines.every(
    (line) =>
      line.trimEnd() === canonical ||
      !/\bLOCKED_SENDER_EMAIL\b\s*(?::|=(?!=)|\+=|-=|\*=|\/\/=|\/=|%=|\*\*=|>>=|<<=|&=|\^=|\|=|@=)/.test(
        line,
      ),
  );
}

function clientValues(body: string): string[] {
  return stringValues(source(`"use client";\n${body}`));
}

function dashboardCandidates(directory = dashboardDirectory): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory())
      return entry.name === "__tests__" ? [] : dashboardCandidates(fullPath);
    return /\.(ts|tsx)$/.test(entry.name) &&
      !/\.d\.ts$|\.test\.tsx?$/.test(entry.name)
      ? [fullPath]
      : [];
  });
}

describe("client-module mailbox guard pure functions", () => {
  it.each([
    [
      "unterminated TypeScript string",
      'const value = "unterminated',
      "synthetic.ts",
    ],
    ["unclosed JSX element", "const value = <section>", "synthetic.tsx"],
  ])("reports parse errors for %s", (_name, text, fileName) =>
    expect(syntaxErrors(text, fileName)).not.toEqual([]),
  );
  it.each([
    ["double directive", '"use client";'],
    ["single directive", "'use client';"],
    ["line comment", '// c\n"use client";'],
    ["block comment", '/* c */\n"use client";'],
    ["BOM", '\uFEFF"use client";'],
    ["trailing comment", '"use client" /* c */;'],
    ["preceding directive", '"other";\n"use client";'],
  ])("recognizes a client directive with %s", (_name, text) =>
    expect(isClientModule(source(text))).toBe(true),
  );
  it.each([
    ["call continuation", '"use client"\n(function () {})();'],
    ["import before directive", 'import value from "x";\n"use client";'],
    ["wrong case", '"USE CLIENT";'],
    ["escaped directive", '"use\\x20client";'],
  ])("does not recognize %s as a client directive", (_name, text) =>
    expect(isClientModule(source(text))).toBe(false),
  );
  it.each([
    ["plain string", 'const x = "name@host.zz";'],
    ["hex escape", 'const x = "name\\x40host.zz";'],
    ["four-digit unicode escape", 'const x = "name\\u0040host.zz";'],
    ["unicode escape", 'const x = "name\\u{40}host.zz";'],
    ["no-substitution template", "const x = `name@host.zz`;"],
    ["template head", "const x = `name@host.zz${value}`;"],
    ["template middle", "const x = `${first}name@host.zz${last}`;"],
    ["template tail", "const x = `${value}name@host.zz`;"],
    ["JSX text", "const x = <p>name@host.zz</p>;"],
    ["JSX decimal entity", "const x = <p>name&#64;host.zz</p>;"],
    ["JSX hexadecimal entity", "const x = <p>name&#x40;host.zz</p>;"],
    ["JSX named entity", "const x = <p>name&commat;host.zz</p>;"],
    ["JSX attribute", 'const x = <p title="name@host.zz" />;'],
    ["JSX attribute entity", 'const x = <p title="name&#64;host.zz" />;'],
    ["JSX expression", 'const x = <p title={"name@host.zz"} />;'],
    ["quoted local", 'const x = "\\\"name\\\"@host.zz";'],
    ["quoted escaped quote", 'const x = "\\\"a\\\\\\\"b\\\"@host.zz";'],
    ["quoted escaped backslash", 'const x = "\\\"a\\\\\\\\\\\"@host.zz";'],
    ["upper case", 'const x = "NAME@HOST.ZZ";'],
    ["trailing period", 'const x = "name@host.zz.";'],
    ["trailing comma", 'const x = "name@host.zz,";'],
    ["mailto prefix", 'const x = "mailto:name@host.zz";'],
    ["maximal domain", 'const x = "n@example.comx";'],
    ["non-reserved suffix", 'const x = "n@host.notexample";'],
    ["reserved prefix", 'const x = "n@example.com.evil.zz";'],
    ["escaped dot", 'const x = "n@example\\x2Ecom.evil.zz";'],
  ])("finds a forbidden mailbox in %s", (_name, body) =>
    expect(
      clientValues(body)
        .flatMap(findMailboxes)
        .some((mailbox) => !isAllowedMailbox(mailbox)),
    ).toBe(true),
  );
  it.each([
    ["reserved subdomain", "name@mail.example.com"],
    ["reserved test suffix", "name@site.test"],
    ["reserved single label", "name@example"],
    ["numeric TLD", "n@example.com9"],
    ["version", "pnpm@10.0.0"],
    ["scope", "@scope/pkg"],
    ["package version", "react@19"],
    ["at rule", "@media"],
    ["platform sender", PLATFORM_SENDER],
    ["platform sender uppercase domain", "zantara@BALIZERO.COM"],
  ])("allows legitimate %s", (_name, value) =>
    expect(findMailboxes(value).every(isAllowedMailbox)).toBe(true),
  );
  it("keeps JSX backslashes literal while ordinary strings decode them", () => {
    expect(
      clientValues(
        'const x = <p title="name\\x40host.zz">name\\x40host.zz</p>;',
      ).flatMap(findMailboxes),
    ).toEqual([]);
    expect(
      clientValues('const x = "name\\x40host.zz";').flatMap(findMailboxes),
    ).toEqual(["name@host.zz"]);
  });
  it("keeps named entities outside the selected subset literal", () =>
    expect(decodeJsxEntities("name&UnderBar;host.zz")).toBe(
      "name&UnderBar;host.zz",
    ));
  it("returns complete mailbox tokens", () => {
    expect(findMailboxes('"a\\"b"@host.zz')).toEqual(['"a\\"b"@host.zz']);
    expect(findMailboxes('"a\\\\"@host.zz')).toEqual(['"a\\\\"@host.zz']);
    expect(findMailboxes("n@example.comx")).toEqual(["n@example.comx"]);
    expect(findMailboxes(`!${PLATFORM_SENDER}`)).toEqual([
      `!${PLATFORM_SENDER}`,
    ]);
  });
  it("requires a complete local token before an exemption", () => {
    expect(isAllowedMailbox(findMailboxes(`!${PLATFORM_SENDER}`)[0])).toBe(
      false,
    );
    expect(isAllowedMailbox(findMailboxes(`(${PLATFORM_SENDER})`)[0])).toBe(
      true,
    );
    expect(
      isAllowedMailbox(findMailboxes(`mailto:${PLATFORM_SENDER}`)[0]),
    ).toBe(true);
  });
  it("does not inspect modules without the client directive or test files", () => {
    expect(isClientModule(source('const x = "name@host.zz";'))).toBe(false);
    const tree = fs.mkdtempSync(path.join(os.tmpdir(), "mailbox-guard-"));
    try {
      const body = '"use client";\nconst x = "name@host.zz";\n';
      fs.mkdirSync(path.join(tree, "__tests__"));
      fs.writeFileSync(path.join(tree, "a.tsx"), body);
      fs.writeFileSync(path.join(tree, "a.test.tsx"), body);
      fs.writeFileSync(path.join(tree, "b.test.ts"), body);
      fs.writeFileSync(path.join(tree, "__tests__", "c.tsx"), body);
      fs.writeFileSync(path.join(tree, "d.d.ts"), body);
      expect(
        dashboardCandidates(tree).map((file) => path.relative(tree, file)),
      ).toEqual(["a.tsx"]);
    } finally {
      fs.rmSync(tree, { recursive: true, force: true });
    }
  });
  it.each([
    ["canonical line", `LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"\n`, true],
    ["no line", "value = 1\n", false],
    ["indented line", `  LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"\n`, false],
    [
      "single quoted second assignment",
      `LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"\nLOCKED_SENDER_EMAIL = 'other@host.zz'\n`,
      false,
    ],
    [
      "annotated second assignment",
      `LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"\nLOCKED_SENDER_EMAIL: str = "other@host.zz"\n`,
      false,
    ],
    [
      "floor-division augmented second assignment",
      `LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"\nLOCKED_SENDER_EMAIL //= "x"\n`,
      false,
    ],
    [
      "augmented second assignment",
      `LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"\nLOCKED_SENDER_EMAIL += "x"\n`,
      false,
    ],
    ["different value", 'LOCKED_SENDER_EMAIL = "other@host.zz"\n', false],
    [
      "symbol read",
      `LOCKED_SENDER_EMAIL = "${PLATFORM_SENDER}"\nvalue = LOCKED_SENDER_EMAIL\n`,
      true,
    ],
  ])("checks sender drift for %s", (_name, text, expected) =>
    expect(checkSenderDrift(text)).toBe(expected),
  );
});

function guardDirectory(directory: string) {
  const candidates = dashboardCandidates(directory);
  const clients: string[] = [];
  const problems: string[] = [];
  for (const fileName of candidates) {
    const text = fs.readFileSync(fileName, "utf8");
    const errors = syntaxErrors(text, fileName);
    if (errors.length) {
      problems.push(...errors);
      continue;
    }
    const sourceFile = source(text, fileName);
    if (!isClientModule(sourceFile)) continue;
    clients.push(path.relative(directory, fileName));
    for (const { value, line } of stringEntries(sourceFile))
      for (const mailbox of findMailboxes(value))
        if (!isAllowedMailbox(mailbox))
          problems.push(`${fileName}:${line}: mailbox is not allowed`);
  }
  return { candidates, clients, problems };
}

describe("client-module mailbox guard walk", () => {
  it("reports a parse failure instead of classifying, and names the line of a mailbox", () => {
    const tree = fs.mkdtempSync(path.join(os.tmpdir(), "mailbox-guard-"));
    try {
      fs.writeFileSync(
        path.join(tree, "a.tsx"),
        '"use client";\nconst ok = 1;\nconst x = "name@host.zz";\n',
      );
      fs.writeFileSync(
        path.join(tree, "b.tsx"),
        '"use client";\nconst x = <p>name@host.zz</p>;\nconst y = <section>\n',
      );
      const { clients, problems } = guardDirectory(tree);
      expect(clients).toEqual(["a.tsx"]);
      expect(
        problems.filter((p) => p.endsWith(": mailbox is not allowed")),
      ).toEqual([`${path.join(tree, "a.tsx")}:3: mailbox is not allowed`]);
      expect(
        problems.some((p) => p.startsWith(`${path.join(tree, "b.tsx")}:`)),
      ).toBe(true);
    } finally {
      fs.rmSync(tree, { recursive: true, force: true });
    }
  });
});

it("guards dashboard client modules and sender drift", () => {
  const { candidates, clients, problems } = guardDirectory(dashboardDirectory);
  console.info(
    `mailbox guard: ${candidates.length} candidates, ${clients.length} client modules`,
  );
  expect(candidates.length).toBeGreaterThanOrEqual(12);
  expect(clients.length).toBeGreaterThanOrEqual(10);
  expect(clients).toContain("page.tsx");
  expect(problems).toEqual([]);
  expect(
    checkSenderDrift(
      fs.readFileSync(
        path.join(
          repositoryDirectory,
          "apps/backend-rag/backend/services/newsletter/publisher.py",
        ),
        "utf8",
      ),
    ),
  ).toBe(true);
});

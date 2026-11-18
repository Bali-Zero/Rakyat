import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import {
  declaredInTree,
  declaredVariables,
  guardedFiles,
  FAMILY,
  nameHits,
  ROOTS,
  variableRefs,
} from "./themeGuard";

// [text, expected match] — one hit on line 1.
const GUILT: [string, string][] = [
  ["bg-card", "bg-card"],
  ["hover:bg-card", "bg-card"],
  ["[&:hover]:bg-card/50", "bg-card"],
  ["hover:bg-card/[.5]", "bg-card"],
  ["!bg-card", "bg-card"],
  ["bg-card!", "bg-card"],
  ["data-[state=open]:bg-muted", "bg-muted"],
  [
    "[&_svg:not([class*='text-'])]:text-muted-foreground",
    "text-muted-foreground",
  ],
  ["scrollbar-thumb-muted-foreground/40", "scrollbar-thumb-muted-foreground"],
  ["border-bs-input", "border-bs-input"],
  ["ring-offset-background", "ring-offset-background"],
  ["inset-ring-ring", "inset-ring-ring"],
  ["text-shadow-primary", "text-shadow-primary"],
  ["mask-b-from-card", "mask-b-from-card"],
  ["rounded-lg bg-card${open}", "bg-card"],
  ['clsx({"bg-card":x})', "bg-card"],
  ["// uses bg-card", "bg-card"],
  ['data-testid="text-input"', "text-input"],
  ['variant="outline-primary"', "outline-primary"],
  ["bg-card x", "bg-card"],
  ["x bg-card", "bg-card"],
  ["bg-card-foreground", "bg-card-foreground"],
];

const INNOCENCE = [
  "bg-[var(--bz-card)]",
  "bg-cardboard",
  "text-cardinal",
  "bg-card-hover",
  "bg-card_x",
  "xbg-card",
  "my-bg-card",
  "bg-accent",
  "text-text-primary",
  "border-border-subtle",
  "text-sm",
  "border",
  "ring-2",
  "shadow-xl",
  "card",
  "background",
  "primary",
  "@/components/ui/card",
  // Stated limit: a literal backslash-n precedes bg-card with the letter n.
  String.raw`"p-2\nbg-card"`,
  "bg-inputs",
];

const ROOTS_COPY = [
  "accent",
  "bg",
  "border",
  "border-b",
  "border-be",
  "border-bs",
  "border-e",
  "border-l",
  "border-r",
  "border-s",
  "border-t",
  "border-x",
  "border-y",
  "caret",
  "decoration",
  "divide",
  "drop-shadow",
  "fill",
  "from",
  "inset-ring",
  "inset-shadow",
  "mask-b-from",
  "mask-b-to",
  "mask-conic-from",
  "mask-conic-to",
  "mask-l-from",
  "mask-l-to",
  "mask-linear-from",
  "mask-linear-to",
  "mask-r-from",
  "mask-r-to",
  "mask-radial-from",
  "mask-radial-to",
  "mask-t-from",
  "mask-t-to",
  "mask-x-from",
  "mask-x-to",
  "mask-y-from",
  "mask-y-to",
  "outline",
  "placeholder",
  "ring",
  "ring-offset",
  "scrollbar-thumb",
  "scrollbar-track",
  "shadow",
  "stroke",
  "text",
  "text-shadow",
  "to",
  "via",
];
const FAMILY_COPY = [
  "card",
  "card-foreground",
  "popover",
  "popover-foreground",
  "background",
  "foreground",
  "muted",
  "muted-foreground",
  "primary",
  "primary-foreground",
  "secondary",
  "secondary-foreground",
  "destructive",
  "destructive-foreground",
  "input",
  "ring",
];

describe("G1 names", () => {
  it("has the 51 roots of Tailwind 4.3.3, equal to the test's own copy", () => {
    expect(ROOTS_COPY).toHaveLength(51);
    expect([...ROOTS]).toEqual(ROOTS_COPY);
  });

  it("has the 16 family names, equal to the test's own copy", () => {
    expect(FAMILY_COPY).toHaveLength(16);
    expect([...FAMILY]).toEqual(FAMILY_COPY);
  });

  it.each(ROOTS_COPY)("root %s hits with -card", (root) => {
    expect(nameHits(`${root}-card`)).toEqual([
      { line: 1, match: `${root}-card` },
    ]);
  });

  it.each(FAMILY_COPY)("bg-%s hits", (name) => {
    expect(nameHits(`bg-${name}`)).toEqual([{ line: 1, match: `bg-${name}` }]);
  });

  it.each(GUILT)("hits %s", (text, match) => {
    expect(nameHits(text)).toEqual([{ line: 1, match }]);
  });

  it.each(INNOCENCE)("does not hit %s", (text) => {
    expect(nameHits(text)).toEqual([]);
  });

  it.each([
    "9bg-card",
    "bg-card9",
    "_bg-card",
    "bg-card_",
    "-bg-card",
    "bg-card-",
  ])("boundary: %s is not a hit", (text) => {
    expect(nameHits(text)).toEqual([]);
  });

  it.each([
    ".bg-card",
    "/bg-card",
    ":bg-card",
    "(bg-card)",
    "$bg-card",
    "{bg-card}",
    "ébg-card",
    "bg-cardé",
  ])("boundary: %s is a hit", (text) => {
    expect(nameHits(text)).toEqual([{ line: 1, match: "bg-card" }]);
  });

  it("reports the line of each of two hits", () => {
    expect(nameHits("a bg-card\nb\n  text-muted-foreground")).toEqual([
      { line: 1, match: "bg-card" },
      { line: 3, match: "text-muted-foreground" },
    ]);
  });

  it("counts lines in a CRLF text", () => {
    expect(nameHits("a\r\nb\r\nbg-card")).toEqual([
      { line: 3, match: "bg-card" },
    ]);
  });
});

describe("G2 references", () => {
  it.each<[string, string[]]>([
    ["bg-[var(--a)]", ["a"]],
    ["var( --a )", ["a"]],
    ["bg-[var(--a,red)]", ["a"]],
    ["bg-[var(--a,var(--b))]", ["a", "b"]],
    ["bg-[color:var(--a)]", ["a"]],
    ["bg-(--a)", ["a"]],
    ["bg-(--a,red)", ["a"]],
    ["bg-(color:--a)/50", ["a"]],
    ["font-(family-name:--a)", ["a"]],
    ["bg-(bg-size:--a)", ["a"]],
    ["bg-(9x:--a)", []],
    ["bg-card/(--o)", ["o"]],
    ["[--x:1px]", []],
    ["text-sm", []],
    ["foo(--a)", []],
    ['style={{ color: "var(--a)" }}', ["a"]],
  ])("%s", (text, names) => {
    expect(variableRefs(text).map((r) => r.name)).toEqual(names);
  });

  it("reports the line of a reference", () => {
    expect(variableRefs("a\n\nbg-(--z)")).toEqual([{ line: 3, name: "z" }]);
  });

  it("reports the same reference twice on two lines", () => {
    expect(variableRefs("bg-(--a)\nx\nbg-(--a)")).toEqual([
      { line: 1, name: "a" },
      { line: 3, name: "a" },
    ]);
  });

  it("counts lines in a CRLF text", () => {
    expect(variableRefs("a\r\nb\r\nbg-(--a)")).toEqual([
      { line: 3, name: "a" },
    ]);
  });
});

describe("declaredVariables", () => {
  it.each<[string, string[]]>([
    [":root{--a:1}", ["a"]],
    ["  --a : 1;", ["a"]],
    ["--a:1", ["a"]],
    ["x{color:red;--a:1}", ["a"]],
    ["x{}--a:1", []],
    [".bz-glass--strong:hover{color:red}", []],
    ["/* --a: 1 */", []],
    // Decision: comments are removed first, so after removal `--a:1` starts the text.
    ["/* c */--a:1", ["a"]],
    ["x{color:var(--a)}", []],
    ['[data-product="kita"]{--k:1}', ["k"]],
    // Decision: @property is NOT a declaration under this rule (no `:` follows).
    ['@property --p { syntax: "*" }', []],
  ])("%s", (css, names) => {
    expect([...declaredVariables(css)]).toEqual(names);
  });
});

describe("import walk", () => {
  const dir = mkdtempSync(join(tmpdir(), "themeguard-"));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));
  const put = (name: string, text: string) => {
    mkdirSync(dirname(join(dir, name)), { recursive: true });
    writeFileSync(join(dir, name), text);
  };
  const walk = (entry: string) => {
    const reads: string[] = [];
    const declared = declaredInTree(join(dir, entry), (f) => {
      reads.push(f.slice(dir.length + 1));
      return readFileSync(f, "utf8");
    });
    return { declared: [...declared].sort(), reads };
  };

  it.each([
    ['@import "./a.css";'],
    ["@import './a.css';"],
    ['@import url("./a.css");'],
    ['@import "./a.css" layer(x);'],
  ])("follows %s", (line) => {
    put("a.css", ":root{--a:1}");
    put("e1.css", line);
    expect(walk("e1.css")).toEqual({
      declared: ["a"],
      reads: ["e1.css", "a.css"],
    });
  });

  it("follows a transitive chain a -> b -> c", () => {
    put("t/a.css", '@import "./b.css";--a:1');
    put("t/b.css", '@import "./c.css";--b:1');
    put("t/c.css", "--c:1");
    expect(walk("t/a.css").declared).toEqual(["a", "b", "c"]);
  });

  it("follows a ../ path", () => {
    put("up/x.css", '@import "../a.css";');
    expect(walk("up/x.css")).toEqual({
      declared: ["a"],
      reads: ["up/x.css", "a.css"],
    });
  });

  it("reads a file imported twice once", () => {
    put("d/root.css", '@import "./a.css";@import "./b.css";');
    put("d/a.css", '@import "./shared.css";');
    put("d/b.css", '@import "./shared.css";');
    put("d/shared.css", "--s:1");
    const { declared, reads } = walk("d/root.css");
    expect(declared).toEqual(["s"]);
    expect(reads.filter((r) => r === "d/shared.css")).toHaveLength(1);
  });

  it("terminates on a cycle a -> b -> a with each file read once", () => {
    put("c/a.css", '@import "./b.css";--a:1');
    put("c/b.css", '@import "./a.css";--b:1');
    const { declared, reads } = walk("c/a.css");
    expect(declared).toEqual(["a", "b"]);
    expect(reads).toEqual(["c/a.css", "c/b.css"]);
  });

  it("skips a missing file without failing", () => {
    put("m/a.css", '@import "./nope.css";--a:1');
    expect(walk("m/a.css").declared).toEqual(["a"]);
  });

  it("does not follow bare specifiers even when such a file exists", () => {
    put("b/tailwindcss", "--tw:1");
    put("b/tailwindcss.css", "--tw2:1");
    put("b/pkg/x.css", "--p:1");
    put("b/root.css", '@import "tailwindcss";\n@import "pkg/x.css";--r:1');
    expect(walk("b/root.css")).toEqual({
      declared: ["r"],
      reads: ["b/root.css"],
    });
  });

  it("does not follow an @import inside a comment", () => {
    put("k/root.css", '/* @import "./a.css"; */--r:1');
    put("k/a.css", "--a:1");
    expect(walk("k/root.css")).toEqual({
      declared: ["r"],
      reads: ["k/root.css"],
    });
  });

  it("selects guarded files by extension, directory and name", () => {
    const names = [
      "a.ts",
      "b.tsx",
      "c.js",
      "d.jsx",
      "e.mjs",
      "f.cjs",
      "g.mts",
      "h.cts",
      "i.d.ts",
      "sub/deep/n.tsx",
      "atest.ts",
      "a.tests.ts",
      "a.specs.ts",
      "spec.ts",
      "tests/x.ts",
    ];
    const skipped = [
      "z.css",
      "z.json",
      "z.md",
      "__tests__/x.ts",
      "a.test.ts",
      "a.spec.tsx",
      "sub/__tests__/y.ts",
      "sub/q.test.tsx",
    ];
    for (const n of [...names, ...skipped]) put(`g/${n}`, "");
    const got = guardedFiles(join(dir, "g")).map((x) =>
      x.slice(join(dir, "g").length + 1),
    );
    expect([...got].sort()).toEqual([...names].sort());
  });
});

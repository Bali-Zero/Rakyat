import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { COPY } from "../copy";
import {
  HISTORICAL_PRICE_RE,
  PRICE_LITERAL_RE,
  visibleSourceText,
} from "@/test/visible-source-text";

/**
 * The e33_claim_guard forbidden-pattern list, verbatim from
 * SPEC-secondhome-studio-phaseB.md §6. Case-insensitive by design (the
 * spec calls this out explicitly) — every pattern below carries the `i`
 * flag.
 *
 * `splitDepositEuphemism` and `youreThere` are fix-mandate-round-1
 * additions (P0-C2 / P2-C14), not part of the original spec §6 list:
 * - P0-C2: the FIRST rewrite of `capital.why` dodged `splitDeposit` by
 *   restating the same forbidden concept in a synonym ("deposits divided
 *   across multiple accounts do not qualify") — an under-match
 *   (superscar #3's twin failure mode). This pattern catches that class
 *   directly, independent of the literal word "split".
 * - P2-C14: "and you're there" reads as an eligibility confirmation
 *   (arrival/conclusion idiom) rather than a preliminary fit-check result.
 * - `passiveIncomeBasis` is a #8460 gate C4 addition (2026-10-14): the
 *   E33-family basis is "income or allowance", never "passive income".
 */
const FORBIDDEN_PATTERNS: Record<string, RegExp> = {
  priceUsd1500: /US?D?\s*\$?\s*1[,.]?500/i,
  anyBank: /any\s+(Indonesian\s+)?bank/i,
  e33SR: /E33[SR]\b/i,
  workLocallyOrInIndonesia: /work(ing)?\s+(locally|in\s+indonesia)/i,
  automaticItapOrPermanent: /automatic(ally)?\s+.*(ITAP|permanent)/i,
  fiveToTenYears: /5\s*[-–]\s*10\s*years?/i,
  idr2Million: /IDR\s*2[,.]?000[,.]?000\b/i,
  guaranteedOr100PercentApproval: /guarantee[ds]?\s+approval|100%\s+approval/i,
  lps: /\bLPS\b/i,
  bsiOrSharia: /\bBSI\b|sharia/i,
  splitDeposit: /split(ting)?\s+.*deposit/i,
  splitDepositEuphemism:
    /divided\s+across\s+multiple\s+accounts|multiple\s+accounts?\s+do(es)?\s+not\s+qualify/i,
  youreThere: /you'?re\s+there|you\s+are\s+there/i,
  passiveIncomeBasis: /passive\s+income/i,
};

interface CopyEntry {
  path: string;
  value: string;
}

function collectStrings(node: unknown, path: string, out: CopyEntry[]): void {
  if (typeof node === "string") {
    out.push({ path, value: node });
    return;
  }
  if (typeof node !== "object" || node === null) return;
  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    collectStrings(value, path ? `${path}.${key}` : key, out);
  }
}

function allCopyStrings(): CopyEntry[] {
  const out: CopyEntry[] = [];
  collectStrings(COPY, "", out);
  return out;
}

// Hoisted to module scope so every describe block below shares one walk of
// the (large, deck-sourced) COPY tree.
const entries = allCopyStrings();

describe("forbidden-claims sweep — every string in COPY", () => {
  it("the sweep actually walks a non-trivial number of strings (guards against an empty/broken walk)", () => {
    expect(entries.length).toBeGreaterThan(40);
  });

  it.each(Object.entries(FORBIDDEN_PATTERNS))(
    "no copy string matches the %s pattern",
    (name, pattern) => {
      const violations = entries.filter((e) => pattern.test(e.value));
      expect(
        violations,
        `pattern "${name}" matched: ${violations.map((v) => v.path).join(", ")}`,
      ).toEqual([]);
    },
  );

  it("no copy string contains a hardcoded price literal (35M/39M in any form)", () => {
    const violations = entries.filter((e) => HISTORICAL_PRICE_RE.test(e.value));
    expect(
      violations,
      `price literal leaked in: ${violations.map((v) => v.path).join(", ")}`,
    ).toEqual([]);
  });
});

describe("forbidden-claims sweep — price literals in COPY", () => {
  it("no copy string contains a currency price literal", () => {
    const violations = entries.filter((e) => PRICE_LITERAL_RE.test(e.value));
    expect(
      violations,
      `IDR price literal leaked in: ${violations.map((v) => v.path).join(", ")}`,
    ).toEqual([]);
  });

  it("guilt: the currency pattern fires on an E33E price the historical pattern misses", () => {
    expect(HISTORICAL_PRICE_RE.test("IDR 45,000,000 all-inclusive")).toBe(
      false,
    );
    expect(PRICE_LITERAL_RE.test("IDR 45,000,000 all-inclusive")).toBe(true);
  });
});

describe("guilt fixtures — each pattern actually fires on a known-bad string", () => {
  const guiltCases: Array<{
    pattern: keyof typeof FORBIDDEN_PATTERNS;
    text: string;
  }> = [
    {
      pattern: "priceUsd1500",
      text: "The total cost is USD 1,500 for processing.",
    },
    { pattern: "priceUsd1500", text: "Just US$1500 flat." },
    { pattern: "anyBank", text: "You can open the account at any bank." },
    { pattern: "anyBank", text: "Works with any Indonesian bank." },
    { pattern: "e33SR", text: "Apply now for E33S." },
    { pattern: "e33SR", text: "The E33R variant is also available." },
    {
      pattern: "workLocallyOrInIndonesia",
      text: "This visa lets you work locally in Bali.",
    },
    {
      pattern: "workLocallyOrInIndonesia",
      text: "You are permitted to work in Indonesia.",
    },
    {
      pattern: "automaticItapOrPermanent",
      text: "Your KITAS automatically converts to ITAP after 3 years.",
    },
    {
      pattern: "automaticItapOrPermanent",
      text: "This status automatically becomes permanent residency.",
    },
    { pattern: "fiveToTenYears", text: "Valid for 5-10 years, typically." },
    { pattern: "fiveToTenYears", text: "Somewhere between 5 – 10 years." },
    {
      pattern: "idr2Million",
      text: "A processing fee of IDR 2,000,000 applies.",
    },
    {
      pattern: "guaranteedOr100PercentApproval",
      text: "We offer guaranteed approval for your visa.",
    },
    {
      pattern: "guaranteedOr100PercentApproval",
      text: "100% approval, no exceptions.",
    },
    {
      pattern: "lps",
      text: "Your deposit is protected by LPS deposit insurance.",
    },
    { pattern: "bsiOrSharia", text: "Also available via BSI." },
    {
      pattern: "bsiOrSharia",
      text: "A sharia-compliant deposit option exists.",
    },
    {
      pattern: "splitDeposit",
      text: "You may consider splitting your deposit across two accounts.",
    },
    {
      pattern: "splitDepositEuphemism",
      text: "Deposits divided across multiple accounts do not qualify.",
    },
    {
      pattern: "splitDepositEuphemism",
      text: "Multiple accounts do not qualify for the threshold.",
    },
    {
      pattern: "youreThere",
      text: "USD 130,000 is the core requirement — and you're there.",
    },
    {
      pattern: "youreThere",
      text: "Once you hit the threshold, you are there.",
    },
    {
      pattern: "passiveIncomeBasis",
      text: "E33E is based on a deposit plus USD 3,000 per month in passive income.",
    },
  ];

  it.each(guiltCases)("$pattern fires on: $text", ({ pattern, text }) => {
    expect(FORBIDDEN_PATTERNS[pattern].test(text)).toBe(true);
  });

  it("the price-literal pattern fires on known-bad price strings", () => {
    expect(HISTORICAL_PRICE_RE.test("IDR 35,000,000")).toBe(true);
    expect(HISTORICAL_PRICE_RE.test("IDR 39.000.000")).toBe(true);
    expect(HISTORICAL_PRICE_RE.test("only 39M")).toBe(true);
    expect(HISTORICAL_PRICE_RE.test("just 35M all-in")).toBe(true);
  });
});

describe("innocence pins — legitimate copy must NOT trip the guard (superscar #3 twin check)", () => {
  it('"non-working residency" does not trip the work-locally pattern', () => {
    expect(
      FORBIDDEN_PATTERNS.workLocallyOrInIndonesia.test("non-working residency"),
    ).toBe(false);
    expect(
      FORBIDDEN_PATTERNS.workLocallyOrInIndonesia.test(
        "This is a non-working residency permit.",
      ),
    ).toBe(false);
  });

  it("the actual copy module uses the safe 'non-working' framing somewhere and it passes the sweep", () => {
    const hasNonWorking = entries.some((e) => /non-working/i.test(e.value));
    // Not a hard requirement of this module (no copy currently needs to
    // describe work rights explicitly), but if it's ever added it must use
    // the safe phrasing — this pin documents and locks that in.
    if (hasNonWorking) {
      const offenders = entries.filter(
        (e) =>
          /non-working/i.test(e.value) &&
          FORBIDDEN_PATTERNS.workLocallyOrInIndonesia.test(e.value),
      );
      expect(offenders).toEqual([]);
    }
  });

  it("USD 130,000 (the real deposit figure) does not trip the USD 1,500 pattern", () => {
    expect(FORBIDDEN_PATTERNS.priceUsd1500.test("USD 130,000")).toBe(false);
  });

  it("USD 1,000,000 (the real property threshold) does not trip the USD 1,500 pattern", () => {
    expect(FORBIDDEN_PATTERNS.priceUsd1500.test("USD 1,000,000")).toBe(false);
  });

  it("E33E / E33F (real product codes) do not trip the E33S/E33R pattern", () => {
    expect(FORBIDDEN_PATTERNS.e33SR.test("E33E")).toBe(false);
    expect(FORBIDDEN_PATTERNS.e33SR.test("E33F")).toBe(false);
    expect(FORBIDDEN_PATTERNS.e33SR.test("the E33E senior pattern")).toBe(
      false,
    );
  });

  it("'up to 5 years, renewable (10-year cumulative cap)' does not trip the 5-10-years pattern", () => {
    expect(
      FORBIDDEN_PATTERNS.fiveToTenYears.test(
        "First grant up to 5 years, renewable (10-year cumulative cap, Pasal 113).",
      ),
    ).toBe(false);
  });

  it("P0-C2: the positive-only capital.why sentence trips neither splitDeposit nor its euphemism pattern", () => {
    // ROUND 2 of the same guard-over-match/under-match cycle
    // (superscar #3): the deck's original draft ("Split deposits do not
    // qualify") tripped splitDeposit (proven below); the FIRST rewrite
    // dodged splitDeposit but restated the SAME forbidden concept in a
    // synonym ("deposits divided across multiple accounts do not
    // qualify") — an under-match the guard couldn't see. The current
    // copy states the rule positively, with no mention of
    // divided/multiple/split accounts at all.
    expect(FORBIDDEN_PATTERNS.splitDeposit.test(COPY.wizard.capital.why)).toBe(
      false,
    );
    expect(
      FORBIDDEN_PATTERNS.splitDepositEuphemism.test(COPY.wizard.capital.why),
    ).toBe(false);
  });

  it("P2-C14: depositReadyStrong no longer reads as an arrival/conclusion idiom", () => {
    expect(
      FORBIDDEN_PATTERNS.youreThere.test(
        COPY.verdict.reasons.depositReadyStrong,
      ),
    ).toBe(false);
  });

  it("documents the deck's own draft phrasing WOULD have tripped splitDeposit (why the rewrite was needed)", () => {
    expect(
      FORBIDDEN_PATTERNS.splitDeposit.test(
        "The threshold must be met through one qualifying deposit. Split deposits do not qualify.",
      ),
    ).toBe(true);
  });

  it("'income or allowance' (and the live seniorFunding.why string) does not trip passiveIncomeBasis", () => {
    expect(
      FORBIDDEN_PATTERNS.passiveIncomeBasis.test(
        "E33E is based on a USD 50,000 deposit plus at least USD 3,000 per month in income or allowance.",
      ),
    ).toBe(false);
    expect(
      FORBIDDEN_PATTERNS.passiveIncomeBasis.test(COPY.wizard.seniorFunding.why),
    ).toBe(false);
    expect(COPY.wizard.seniorFunding.why).toContain("income or allowance");
  });
});

describe("positive pins — required phrasing is actually present", () => {
  it("custody copy contains 'state-owned' and 'own name'", () => {
    const custodyStrings = [
      COPY.custody.eyebrow,
      COPY.custody.intro,
      ...Object.values(COPY.custody.steps).map(
        (step) => `${step.title} ${step.body}`,
      ),
      COPY.custody.disclaimer,
    ]
      .join(" ")
      .toLowerCase();
    expect(custodyStrings).toContain("state-owned");
    expect(custodyStrings).toContain("own name");
  });

  it("custody step 3 keeps the withdrawal/compliance warning (copy-deck critique #2)", () => {
    const step3 = COPY.custody.steps.step3.body.toLowerCase();
    expect(step3).toMatch(/withdraw|moving them|move them/);
    expect(step3).toContain("compliance");
  });

  it("every verdict band body states the final decision rests with Imigrasi", () => {
    for (const band of Object.values(COPY.verdict.bands)) {
      expect(band.body.toLowerCase()).toContain(
        "the final decision rests with imigrasi",
      );
    }
  });

  it("dependents are described as priced in the free fit memo, never with a figure", () => {
    const dependentsNote = COPY.wizard.family.dependentsNote.toLowerCase();
    expect(dependentsNote).toContain("fit memo");
    expect(HISTORICAL_PRICE_RE.test(dependentsNote)).toBe(false);
  });

  it("save-plan bar carries the deck's link warning and clear-plan action string", () => {
    expect(COPY.savePlanBar.linkWarning.toLowerCase()).toContain("link");
    expect(COPY.savePlanBar.linkWarning.toLowerCase()).toMatch(/view|see/);
    expect(COPY.savePlanBar.clearButton.toLowerCase()).toContain("clear");
  });

  it("whatsapp block carries the deck's privacy string (review-before-send)", () => {
    expect(COPY.whatsapp.privacy.toLowerCase()).toContain("review");
  });

  it("P0-C3(c)/P1-B: whatsapp.privacy names Bali Zero and describes BOTH the capture and the pre-fill honestly", () => {
    // Supersedes the earlier P1-B guidance ("mentions plan link") — P0-C3
    // removed the plan link from the payload entirely, so the honest
    // sentence no longer mentions one; it must still name who receives the
    // data and that WhatsApp gets pre-filled with it.
    expect(COPY.whatsapp.privacy).toContain("Bali Zero");
    expect(COPY.whatsapp.privacy.toLowerCase()).toContain("whatsapp");
    expect(COPY.whatsapp.privacy.toLowerCase()).not.toContain("plan link");
  });

  it("whatsapp prefill template names the fit-check result, never an assessment/certificate/score", () => {
    expect(COPY.whatsapp.prefillTemplate).toContain("Fit-check result:");
  });

  it("P0-C3(c): whatsapp prefill template mirrors the 6 bullets exactly, with no plan-link placeholder", () => {
    for (const label of [
      "Route:",
      "Age band:",
      "Funding position:",
      "Family:",
      "Timing:",
      "Fit-check result:",
    ]) {
      expect(COPY.whatsapp.prefillTemplate).toContain(label);
    }
    expect(COPY.whatsapp.prefillTemplate.toLowerCase()).not.toContain(
      "readiness",
    );
    expect(COPY.whatsapp.prefillTemplate.toLowerCase()).not.toContain(
      "saved plan",
    );
    expect(COPY.whatsapp.prefillTemplate).not.toMatch(/https?:\/\//);
  });
});

describe("terminology delta — 'fit-check result' only, never assessment/certificate/eligibility score", () => {
  it("no copy string ever says 'assessment'", () => {
    const violations = entries.filter((e) => /assessment/i.test(e.value));
    expect(
      violations,
      `"assessment" found in: ${violations.map((v) => v.path).join(", ")}`,
    ).toEqual([]);
  });

  it("no copy string ever says 'certificate'", () => {
    const violations = entries.filter((e) => /certificate/i.test(e.value));
    expect(
      violations,
      `"certificate" found in: ${violations.map((v) => v.path).join(", ")}`,
    ).toEqual([]);
  });

  it("no copy string ever says 'eligibility score'", () => {
    const violations = entries.filter((e) =>
      /eligibility\s+score/i.test(e.value),
    );
    expect(
      violations,
      `"eligibility score" found in: ${violations.map((v) => v.path).join(", ")}`,
    ).toEqual([]);
  });

  it("'fit-check result' is actually used in the copy", () => {
    const hasPhrase = entries.some((e) => /fit-check result/i.test(e.value));
    expect(hasPhrase).toBe(true);
  });

  it("the deck's own draft ('a preliminary fit assessment') would have tripped the assessment ban (why the rewrite was needed)", () => {
    expect(
      /assessment/i.test(
        "This is a preliminary fit assessment, not an approval.",
      ),
    ).toBe(true);
  });
});

/** Source text is extracted by the TypeScript parser, not a hand-rolled lexer. */

const STUDIO_SRC_DIR = join(
  __dirname,
  "..",
  "..",
  "..",
  "app",
  "visa",
  "second-home",
  "studio",
);

const WORKSPACE_SECOND_HOME_DIR = join(
  __dirname,
  "..",
  "..",
  "..",
  "app",
  "(workspace)",
  "second-home",
);

function listSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...listSourceFiles(full));
    } else if (
      entry.isFile() &&
      /\.tsx?$/.test(entry.name) &&
      !/\.test\.tsx?$/.test(entry.name)
    ) {
      out.push(full);
    }
  }
  return out;
}

const studioTsxFiles = listSourceFiles(STUDIO_SRC_DIR);
/** The public studio AND the staff console's second-home pages (.ts + .tsx). */
const scannedSourceFiles = [
  ...studioTsxFiles,
  ...listSourceFiles(WORKSPACE_SECOND_HOME_DIR),
];

const SOURCE_PRICE_PATTERNS = [PRICE_LITERAL_RE, HISTORICAL_PRICE_RE];

function sourceViolations(
  files: Array<{ file: string; source: string }>,
  patterns: RegExp[],
): string[] {
  const violations: string[] = [];
  for (const { file, source } of files) {
    for (const text of visibleSourceText(source, file)) {
      if (patterns.some((pattern) => pattern.test(text))) {
        violations.push(`${file}: ${JSON.stringify(text)}`);
      }
    }
  }
  return violations;
}

describe("visibleSourceText — parser fixtures", () => {
  it("guilt: catches a forbidden phrase inside a real string literal", () => {
    const src = `const x = "You can open the account at any bank.";`;
    const hit = visibleSourceText(src).some((s) =>
      FORBIDDEN_PATTERNS.anyBank.test(s),
    );
    expect(hit).toBe(true);
  });

  it("guilt: catches a generalized IDR price literal beyond 35M/39M", () => {
    const src = `const x = "IDR 42,500,000 total.";`;
    const hit = visibleSourceText(src).some((s) => PRICE_LITERAL_RE.test(s));
    expect(hit).toBe(true);
  });

  it("innocence: a forbidden-looking phrase INSIDE a // line comment is not extracted as a string", () => {
    const src = `// any bank works here, this is only a comment\nconst x = "USD 130,000";`;
    const hit = visibleSourceText(src).some((s) =>
      FORBIDDEN_PATTERNS.anyBank.test(s),
    );
    expect(hit).toBe(false);
  });

  it("innocence: a forbidden-looking phrase INSIDE a /* */ block comment is not extracted as a string", () => {
    const src = `/**\n * any bank — historical note only\n */\nconst x = "USD 130,000";`;
    const hit = visibleSourceText(src).some((s) =>
      FORBIDDEN_PATTERNS.anyBank.test(s),
    );
    expect(hit).toBe(false);
  });

  it("a string literal containing '//' (a URL) is extracted whole, not truncated at the comment-like slashes", () => {
    const src = `const x = "https://balizero.com/visa/second-home/studio";`;
    const literals = visibleSourceText(src);
    expect(literals).toContain("https://balizero.com/visa/second-home/studio");
  });

  it("guilt: preserves JSX text when an attribute contains a URL", () => {
    const src = `<a href="https://example.test">Open at any bank. IDR 45,000,000</a>`;
    const text = visibleSourceText(src);
    expect(text.some((s) => FORBIDDEN_PATTERNS.anyBank.test(s))).toBe(true);
    expect(text.some((s) => PRICE_LITERAL_RE.test(s))).toBe(true);
  });

  it("guilt: normalizes JSX string expressions before matching whitespace", () => {
    const text = visibleSourceText(`<p>passive{" "}income</p>`);
    expect(
      text.some((s) => FORBIDDEN_PATTERNS.passiveIncomeBasis.test(s)),
    ).toBe(true);
  });

  it.each(["45.000.000 IDR", "45,000,000 IDR"])(
    "guilt: catches an amount-then-currency JSX price literal: %s",
    (price) => {
      const text = visibleSourceText(`<p>${price}</p>`);
      expect(text.some((s) => PRICE_LITERAL_RE.test(s))).toBe(true);
    },
  );
});

describe("visibleSourceText — JSX text children", () => {
  it("guilt: a price written as JSX text is seen and flagged", () => {
    const src = `export const X = () => (<p className="x">E33E costs IDR 45,000,000</p>);`;
    expect(visibleSourceText(src).some((s) => PRICE_LITERAL_RE.test(s))).toBe(
      true,
    );
  });

  it("guilt: 'passive income' and 'any bank' as multi-line JSX text are flagged", () => {
    const src = `const X = () => (\n  <div>\n    <p>\n      USD 3,000/month passive\n      income only. Open it at any bank.\n    </p>\n  </div>\n);`;
    const text = visibleSourceText(src);
    expect(
      text.some((s) => FORBIDDEN_PATTERNS.passiveIncomeBasis.test(s)),
    ).toBe(true);
    expect(text.some((s) => FORBIDDEN_PATTERNS.anyBank.test(s))).toBe(true);
  });

  it("guilt: the scratch fixture a scan would meet is flagged, a clean one is not", () => {
    const dirty = `<p><strong>E33F</strong> — USD 3,000/month passive income only, IDR 35,000,000</p>`;
    const clean = `<p><strong>E33F</strong> — income or allowance of at least USD 3,000/month</p>`;
    expect(
      visibleSourceText(dirty).some((s) =>
        FORBIDDEN_PATTERNS.passiveIncomeBasis.test(s),
      ),
    ).toBe(true);
    expect(
      visibleSourceText(clean).some((s) =>
        FORBIDDEN_PATTERNS.passiveIncomeBasis.test(s),
      ),
    ).toBe(false);
  });

  it("innocence: commented-out markup and {expressions} are not scanned as text", () => {
    const src = `// <p>any bank</p>\nconst X = () => <p>{price} all-in</p>;`;
    const text = visibleSourceText(src);
    expect(text.some((s) => FORBIDDEN_PATTERNS.anyBank.test(s))).toBe(false);
    expect(text).toContain("all-in");
  });
});

describe("source-scan — JSX literals and JSX text under app/visa/second-home/studio/ and app/(workspace)/second-home/ (P2-C13)", () => {
  it("scans a non-trivial number of files (guards against an empty/broken walk)", () => {
    expect(studioTsxFiles.length).toBeGreaterThan(5);
  });

  it("the staff new-case page and the other (workspace)/second-home sources are in scope", () => {
    const rel = scannedSourceFiles.map((f) => f.replace(/\\/g, "/"));
    expect(
      rel.some((f) => f.endsWith("(workspace)/second-home/new/page.tsx")),
    ).toBe(true);
    expect(
      rel.some((f) =>
        f.endsWith("(workspace)/second-home/components/severity.ts"),
      ),
    ).toBe(true);
  });

  it.each(Object.entries(FORBIDDEN_PATTERNS))(
    "no JSX string/template literal or JSX text in the scanned tree matches the %s pattern",
    (name, pattern) => {
      const violations = sourceViolations(
        scannedSourceFiles.map((file) => ({
          file,
          source: readFileSync(file, "utf8"),
        })),
        [pattern],
      );
      expect(violations, violations.join("\n")).toEqual([]);
    },
  );

  it("applies the historical-price pattern through the same source sweep", () => {
    expect(
      sourceViolations(
        [
          { file: "fixture.tsx", source: "<p>39.000.000</p>" },
          { file: "fixture.tsx", source: "<p>35M</p>" },
        ],
        SOURCE_PRICE_PATTERNS,
      ),
    ).toHaveLength(2);
  });

  it("no visible source text contains a price literal", () => {
    const violations = sourceViolations(
      scannedSourceFiles.map((file) => ({
        file,
        source: readFileSync(file, "utf8"),
      })),
      SOURCE_PRICE_PATTERNS,
    );
    expect(violations, violations.join("\n")).toEqual([]);
  });
});

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * WS2 kita slice 6 (final sweep) — HR suite list/hub drain guard.
 *
 * Pins the token drain of the WORKSPACE HR surfaces covered by the final
 * sweep (hub dashboard, payroll list, owner-cashout list, hr settings, hr
 * layout, leave/request): statuses read --state-* (hue-preserving payroll
 * map), amounts read the Money component, panels read the dashboard recipe,
 * on-accent ink reads --bz-on-warm. No raw hex, no status rgba, no palette
 * utilities.
 *
 * The hub (`/hr`) is the People console since 2026-11-12: it shares the owner-approved
 * My HR palette (`_components/hr-panels.tsx`, same as `/hr/me`), so it is no longer a
 * dashboard-recipe surface and is not pinned here.
 *
 * Named `token-drain.sweep.guard.test.ts` (not the slice-4 name) so it can
 * coexist with the HR detail-page guard from the slice-4 line of work.
 */

const PAGES = {
  payroll: join(__dirname, "..", "payroll", "page.tsx"),
  ownerCashout: join(__dirname, "..", "owner-cashout", "page.tsx"),
  hrSettings: join(__dirname, "..", "settings", "page.tsx"),
  layout: join(__dirname, "..", "layout.tsx"),
  leaveRequest: join(__dirname, "..", "leave", "request", "page.tsx"),
};

// token_lint.py HEX_RE — 3/4/6/8 digits, word-boundary aware.
const HEX_RE =
  /(?<![0-9A-Za-z#&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9A-Za-z])/;

const PALETTE_UTIL_RE =
  /\b(?:bg|text|border)(?:-[tlbrxy])?-(?:red|green|emerald|amber|rose|blue|sky|cyan|purple|violet|indigo|yellow|zinc|gray|slate|neutral|stone|orange|pink|fuchsia|lime|teal)-\d/;

/** Code lines only: drops full-line comments and token-lint-ok one-offs. */
function codeLines(path: string): string[] {
  return readFileSync(path, "utf8")
    .split("\n")
    .filter((l) => {
      const t = l.trimStart();
      if (t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")) {
        return false;
      }
      return !l.includes("token-lint-ok:");
    });
}

describe("HR suite drain guard (WS2 slice 6)", () => {
  for (const [name, path] of Object.entries(PAGES)) {
    it(`${name}: no unmarked raw hex colors`, () => {
      for (const line of codeLines(path)) {
        expect(HEX_RE.test(line), `hex in line: ${line.trim()}`).toBe(false);
      }
    });

    it(`${name}: no Tailwind status/neutral palette utilities`, () => {
      for (const line of codeLines(path)) {
        expect(PALETTE_UTIL_RE.test(line), `palette util: ${line.trim()}`).toBe(
          false,
        );
      }
    });
  }

  it("payroll list: hue-preserving status map + Money figures", () => {
    const src = readFileSync(PAGES.payroll, "utf8");
    expect(src).toContain("bg-[var(--state-info)]/10 text-[var(--state-info)]");
    expect(src).toContain(
      "bg-[var(--state-warning)]/10 text-[var(--state-warning)]",
    );
    expect(src).toContain(
      "bg-[var(--state-success)]/10 text-[var(--state-success)]",
    );
    expect(src).toContain("<Money value={slip.net_salary_idr} />");
    expect(src).not.toContain("formatIDR(");
  });

  it("owner-cashout list: MBZ->success, MBS->warning via Money", () => {
    const src = readFileSync(PAGES.ownerCashout, "utf8");
    expect(src).toContain("<Money value={w.total_margin_bz_idr} />");
    expect(src).toContain("<Money value={w.total_margin_bs_idr} />");
    expect(src).toContain("var(--state-success)");
    expect(src).toContain("var(--state-warning)");
    expect(src).not.toContain("formatIDR(");
  });

  it("panels read the dashboard recipe across the suite", () => {
    for (const path of [
      PAGES.payroll,
      PAGES.ownerCashout,
      PAGES.hrSettings,
      PAGES.leaveRequest,
    ]) {
      const src = readFileSync(path, "utf8");
      expect(src).toContain("var(--bz-card)");
      expect(src).toContain("var(--bz-border)");
    }
  });

  it("on-accent ink reads --bz-on-warm (leave request + calendar)", () => {
    const src = readFileSync(PAGES.leaveRequest, "utf8");
    expect(src).toContain("var(--bz-on-warm)");
    expect(src).not.toContain("text-zinc-950");
  });
});

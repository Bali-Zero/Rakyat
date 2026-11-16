import { existsSync } from "node:fs";
import { join } from "node:path";
import { describe, it, expect } from "vitest";
import {
  TEAM_ROSTER,
  rosterByDept,
  rosterBySlug,
  type TeamDept,
} from "./team-roster";
import { bookTeamMembers } from "@/components/book/book-team";
import { TRANSLATIONS } from "@/components/book/book-data";

const NEW_DEPTS: TeamDept[] = ["immigration", "operations", "security"];

const GROUP_STAFF: Array<[string, TeamDept]> = [
  ["dian", "leadership"],
  ["adi", "leadership"],
  ["agus", "immigration"],
  ["farid", "immigration"],
  ["nanang", "immigration"],
  ["yoyok", "operations"],
  ["yudi", "operations"],
  ["megi", "operations"],
  ["david", "operations"],
  ["novi", "operations"],
  ["lia", "operations"],
  ["yanti", "operations"],
  ["oman", "accounting"],
  ["dini", "accounting"],
  ["ropik", "security"],
];

describe("team roster — group staff portraits", () => {
  it("has unique slugs and unique emails", () => {
    const slugs = TEAM_ROSTER.map((m) => m.slug);
    const emails = TEAM_ROSTER.map((m) => m.email.toLowerCase());
    expect(new Set(slugs).size).toBe(slugs.length);
    expect(new Set(emails).size).toBe(emails.length);
  });

  it.each(GROUP_STAFF)("lists %s under %s with a shipped portrait", (slug, dept) => {
    const m = rosterBySlug(slug);
    expect(m, `${slug} missing from the roster`).toBeDefined();
    expect(m?.dept).toBe(dept);
    expect(m?.photo).toBe(`/static/team/${slug}.jpg`);
    expect(
      existsSync(join(__dirname, "..", "..", "public", String(m?.photo))),
    ).toBe(true);
  });

  it("points Zainal and Heru at the new A1 portraits and keeps the old files", () => {
    for (const slug of ["zainal", "heru"]) {
      const photo = String(rosterBySlug(slug)?.photo);
      expect(photo).toBe(`/static/team/${slug}-a1.jpg`);
      expect(existsSync(join(__dirname, "..", "..", "public", photo))).toBe(
        true,
      );
    }
    for (const old of [
      "zainal-ceo.jpg",
      "heru-komisaris.jpg",
      "zainal-heru-together.jpg",
    ]) {
      expect(existsSync(join(__dirname, "..", "..", "public", "static", "team", old))).toBe(true);
    }
    expect(rosterBySlug("zainal")?.role).toBe("CEO");
    expect(rosterBySlug("heru")?.role).toBe("Commissioner");
  });

  it("fills each new department", () => {
    for (const dept of NEW_DEPTS) {
      expect(rosterByDept(dept).length, dept).toBeGreaterThan(0);
    }
  });

  it("gives every public dept a label in every book language", () => {
    const depts = new Set(bookTeamMembers().map((m) => m.department));
    for (const dept of NEW_DEPTS) expect(depts.has(dept), dept).toBe(true);
    for (const [lang, t] of Object.entries(TRANSLATIONS)) {
      for (const dept of depts) {
        expect(t.teamDepts[dept], `${lang}.teamDepts.${dept}`).toBeTruthy();
      }
    }
  });
});

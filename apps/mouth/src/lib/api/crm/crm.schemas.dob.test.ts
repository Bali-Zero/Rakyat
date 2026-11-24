import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createClientSchema } from "./crm.schemas";

const base = { full_name: "Person One" };

function dobMessages(dob: string | undefined): string[] {
  const r = createClientSchema.safeParse({ ...base, date_of_birth: dob });
  if (r.success) return [];
  return r.error.issues
    .filter((i) => i.path[0] === "date_of_birth")
    .map((i) => i.message);
}

describe("createClientSchema date of birth", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 5, 15, 12, 0, 0));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("accepts a person whose eighteenth birthday is today", () => {
    expect(dobMessages("2008-06-15")).toEqual([]);
  });

  it("rejects a person turning eighteen tomorrow with the minor message", () => {
    const m = dobMessages("2008-06-16");
    expect(m).toHaveLength(1);
    expect(m[0]).toContain("Client is 17 years old");
  });

  it("rejects a future date without printing an age", () => {
    const m = dobMessages("2030-01-01");
    expect(m).toHaveLength(1);
    expect(m[0]).not.toMatch(/-\d/);
    expect(m[0]).not.toContain("years old");
  });

  it("rejects dates that are not real calendar dates", () => {
    expect(dobMessages("2000-04-31")).toHaveLength(1);
    expect(dobMessages("2001-02-29")).toHaveLength(1);
    expect(dobMessages("2000-13-01")).toHaveLength(1);
  });

  it("accepts an adult and a leap-day birthday", () => {
    expect(dobMessages("1990-01-15")).toEqual([]);
    expect(dobMessages("2000-02-29")).toEqual([]);
  });

  it("keeps the minor message for a seventeen-year-old", () => {
    const m = dobMessages("2009-01-01");
    expect(m).toHaveLength(1);
    expect(m[0]).toMatch(
      /^Client is 17 years old\. Minors cannot have a standalone profile/,
    );
  });

  it("treats an empty or missing date as before", () => {
    expect(dobMessages("")).toEqual([]);
    expect(dobMessages(undefined)).toEqual([]);
  });
});

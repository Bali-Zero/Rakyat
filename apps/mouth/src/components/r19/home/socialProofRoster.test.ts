import { describe, it, expect, beforeEach, vi } from "vitest";

// Moved from the retired SocialProof.test.tsx: the exclusion filter must stay
// wired into the resolver that the home Team band reads.
const extraExcluded = new Set<string>();

vi.mock("@/lib/team-public-listing", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/team-public-listing")>();
  return {
    ...actual,
    publicEntries: <T extends { slug?: string }>(entries: readonly T[]) =>
      actual
        .publicEntries(entries)
        .filter((e) => !e.slug || !extraExcluded.has(e.slug)),
  };
});

async function roster() {
  const { socialProofRoster } = await import("./socialProofRoster");
  return socialProofRoster();
}

beforeEach(() => {
  vi.resetModules();
  extraExcluded.clear();
});

describe("socialProofRoster — the exclusion filter is wired into the resolver", () => {
  it("still resolves the founders and the three people behind them", async () => {
    const { founders, team } = await roster();
    expect(founders.map((m) => m.name)).toEqual(["Zainal Abidin", "Pak Heru"]);
    expect(team.map((m) => m.name)).toEqual(["Ruslana", "Adit", "Angel"]);
  });

  it("publishes neither excluded person", async () => {
    expect(JSON.stringify(await roster())).not.toMatch(/faisha|faysha|sahira/i);
  });

  it("drops a person the filter excludes, whoever it is", async () => {
    extraExcluded.add("adit");
    const { team } = await roster();
    expect(team.map((m) => m.name)).not.toContain("Adit");
    expect(team.map((m) => m.name)).toContain("Angel");
  });

  it("applies the filter to the founders too", async () => {
    extraExcluded.add("heru");
    const { founders } = await roster();
    expect(founders.map((m) => m.name)).not.toContain("Pak Heru");
    expect(founders.map((m) => m.name)).toContain("Zainal Abidin");
  });
});

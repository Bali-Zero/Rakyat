import { describe, expect, it } from "vitest";
import type { ClientProfile } from "@/lib/api/crm/crm.types";
import { supersededVisaPracticeIds } from "./supersededVisaPractice";

type P = ClientProfile["practices"][number];
const p = (over: Partial<P> & { id: number }): P =>
  ({
    status: "completed",
    practice_type_code: "visa_b211a_tourist",
    practice_type_name: "Tourist Visa",
    ...over,
  }) as P;

// Synthetic shape of client 10247 (6 Oct): a completed tourist visa (practice 41)
// replaced by a completed KITAS (practice 311) for the same person.
describe("supersededVisaPracticeIds", () => {
  it("GUILT: a completed visa replaced by a later KITAS for the same person is superseded", () => {
    const ids = supersededVisaPracticeIds([
      p({ id: 41 }),
      p({
        id: 311,
        practice_type_code: "kitas_investor_2yr_onshore",
        practice_type_name: "KITAS Investor",
      }),
    ]);
    expect([...ids]).toEqual([41]);
  });

  it("a later visa still in process also supersedes the old completed one", () => {
    const ids = supersededVisaPracticeIds([
      p({ id: 41 }),
      p({ id: 311, status: "on_process", practice_type_code: "kitas_x" }),
    ]);
    expect(ids.has(41)).toBe(true);
  });

  it("INNOCENCE: a cancelled later visa does not supersede", () => {
    const ids = supersededVisaPracticeIds([
      p({ id: 41 }),
      p({ id: 311, status: "cancelled", practice_type_code: "kitas_x" }),
    ]);
    expect(ids.size).toBe(0);
  });

  it("INNOCENCE: a family member's visa never supersedes the client's own", () => {
    const ids = supersededVisaPracticeIds([
      p({ id: 41 }),
      p({ id: 311, family_member_id: 9, practice_type_code: "kitas_x" }),
    ]);
    expect(ids.size).toBe(0);
  });

  it("INNOCENCE: the latest visa and non-visa practices are never superseded", () => {
    const ids = supersededVisaPracticeIds([
      p({ id: 41 }),
      p({
        id: 50,
        practice_type_code: "pt_setup",
        practice_type_name: "PT PMA Setup",
      }),
      p({
        id: 60,
        practice_type_code: "pt_setup_2",
        practice_type_name: "PT PMA Setup",
      }),
    ]);
    expect(ids.size).toBe(0);
  });

  it("an older visa still in process is not marked (only completed ones are)", () => {
    const ids = supersededVisaPracticeIds([
      p({ id: 41, status: "on_process" }),
      p({ id: 311, practice_type_code: "kitas_x" }),
    ]);
    expect(ids.size).toBe(0);
  });
});

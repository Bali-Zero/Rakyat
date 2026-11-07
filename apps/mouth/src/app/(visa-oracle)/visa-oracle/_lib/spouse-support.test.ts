import { describe, expect, it } from "vitest";
import { runWalk } from "../../../../../scripts/visa-oracle/generate-walk-corpus";
import { mapOracleFactsToApplicantFacts } from "./fact-mapper";
import { initialFlowState, flowReducer } from "./flow";

describe("spouse letter and contradictory company answers", () => {
  it.each([
    ["yes", { status: "KNOWN", value: true }],
    ["no", { status: "KNOWN", value: false }],
    ["pending", { status: "UNKNOWN", reason: "UNVERIFIED" }],
  ])(
    "keeps the explicit spouse answer %s independent of generic family agreement",
    (value, expected) => {
      const { facts, asked } = runWalk({
        category: "family",
        family_relation: "SPOUSE",
        marital_status: "MARRIED",
        family_sponsor_nationalities: "ID",
        family_marriage_registered: "yes",
        family_sponsor_confirmed: "no",
        family_spouse_application_support_confirmed: value as string,
      });
      expect(asked).toContain("family_spouse_application_support_confirmed");
      const wire = mapOracleFactsToApplicantFacts(facts, {
        assessmentId: "00000000-0000-4000-8000-000000000001",
        collectedAt: new Date("2026-11-04T00:00:00Z"),
      });
      expect(wire.facts["family.spouse_application_support_confirmed"]).toEqual(
        expected,
      );
    },
  );
  it("preserves the company question until the contradictory sponsor selection can be corrected", () => {
    const state = {
      ...initialFlowState("en"),
      history: [
        { kind: "question" as const, questionId: "sponsor_category" },
        {
          kind: "question" as const,
          questionId: "investment_sponsoring_company_confirmed",
        },
      ],
      facts: { sponsor_category: "NONE" },
    };
    const blocked = flowReducer(state, {
      type: "ANSWER",
      questionId: "investment_sponsoring_company_confirmed",
      value: "yes",
    });
    expect(blocked.facts).toEqual(state.facts);
    expect(blocked.blockedAnswer?.conflictsWithQuestionId).toBe(
      "sponsor_category",
    );
    const back = flowReducer(blocked, { type: "BACK" });
    expect(back.blockedAnswer).toBeNull();
    expect(back.history[back.history.length - 1]).toEqual({
      kind: "question",
      questionId: "sponsor_category",
    });
  });
});

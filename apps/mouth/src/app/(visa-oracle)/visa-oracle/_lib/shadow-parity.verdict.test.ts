import { describe, expect, it } from "vitest";
import { buildPreviewOutcome } from "./preview-adapter";
import { shadowParityVerdict } from "./shadow-parity";
import { makeVisaOracleResponse } from "./visa-oracle-test-fixture";

const CLOCK = new Date("2026-08-03T04:00:00Z");

const PINNED_FACTS = {
  category: "remote",
  work_payer: "unsure",
  remote_compensation: "no",
  remote_clients: "foreign",
  review_gate: "none",
};

function humanReviewResponse(code: string) {
  const response = makeVisaOracleResponse("HUMAN_REVIEW_REQUIRED");
  response.decision.review_reasons = [
    { code, rule_ids: ["system.synthetic-rule"], source_refs: [] },
  ];
  response.sources = [];
  return response;
}

describe("shadow parity verdict", () => {
  it("reports no baseline when the visitor matches no pinned persona", () => {
    const baseline = buildPreviewOutcome({}, CLOCK);
    const response = makeVisaOracleResponse("TEMPORARILY_UNAVAILABLE");
    expect(shadowParityVerdict(response, baseline)).toBe("no_baseline");
    expect(
      shadowParityVerdict(
        makeVisaOracleResponse("SUPPORTED_CANDIDATES"),
        baseline,
      ),
    ).toBe("no_baseline");
  });

  it("reports a match when a pinned persona equals the engine answer", () => {
    const baseline = buildPreviewOutcome(PINNED_FACTS, CLOCK);
    expect(
      shadowParityVerdict(
        humanReviewResponse("DISCLOSED_UNCERTAINTY_REVIEW"),
        baseline,
      ),
    ).toBe("match");
  });

  it("reports a mismatch when a pinned persona differs from the engine answer", () => {
    const baseline = buildPreviewOutcome(PINNED_FACTS, CLOCK);
    expect(
      shadowParityVerdict(
        humanReviewResponse("DISCLOSED_ACTIVITY_BOUNDARY_REVIEW"),
        baseline,
      ),
    ).toBe("mismatch");
  });
});

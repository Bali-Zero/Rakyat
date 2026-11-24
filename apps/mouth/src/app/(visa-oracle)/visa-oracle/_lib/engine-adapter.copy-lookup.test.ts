import { afterEach, describe, expect, it, vi } from "vitest";

const emitVisaOracleTelemetry = vi.hoisted(() => vi.fn());
vi.mock("./telemetry", () => ({ emitVisaOracleTelemetry }));

import {
  buildEngineOutcome,
  GENERIC_NOTICE_CONDITION,
  NOTICE_CONDITION_COPY,
  UNMAPPED_REASON_COPY,
} from "./engine-adapter";
import { VisaOracleResponseError } from "./engine-response";
import { makeVisaOracleResponse } from "./visa-oracle-test-fixture";

const GENERIC_REVIEW_EN =
  "Some of your answers need a review before a path can be confirmed. A Bali Zero advisor can go over them with you.";

const INHERITED_NAMES = [
  "constructor",
  "toString",
  "hasOwnProperty",
  "__proto__",
];

afterEach(() => {
  vi.unstubAllEnvs();
  emitVisaOracleTelemetry.mockClear();
});

describe("copy tables resolve only their own keys", () => {
  it.each(INHERITED_NAMES)(
    "takes the unmapped support branch for the reason code %s",
    (code) => {
      const response = makeVisaOracleResponse();
      response.decision.candidates[0].reason_codes = [code];
      const outcome = buildEngineOutcome(response);
      if (outcome.state !== "SUPPORTED_CANDIDATES")
        throw new Error("unexpected state");
      expect(outcome.candidates[0].legal.reasons[0].message).toBe(
        UNMAPPED_REASON_COPY,
      );
    },
  );

  it.each(INHERITED_NAMES)(
    "throws for the review code %s outside production",
    (code) => {
      const response = makeVisaOracleResponse("HUMAN_REVIEW_REQUIRED");
      response.decision.review_reasons[0].code = code;
      expect(() => buildEngineOutcome(response)).toThrow(
        new VisaOracleResponseError("RESPONSE_INVARIANT"),
      );
    },
  );

  it.each(INHERITED_NAMES)(
    "reports the review code %s as unmapped in production",
    (code) => {
      vi.stubEnv("NODE_ENV", "production");
      const response = makeVisaOracleResponse("HUMAN_REVIEW_REQUIRED");
      response.decision.review_reasons[0].code = code;
      const outcome = buildEngineOutcome(response);
      if (outcome.state !== "HUMAN_REVIEW_REQUIRED")
        throw new Error("unexpected state");
      expect(outcome.reviewReasons[0].message.en).toBe(GENERIC_REVIEW_EN);
      expect(emitVisaOracleTelemetry).toHaveBeenCalledWith({
        event: "visa_oracle_v2_review_reason_unmapped_code",
        code,
      });
    },
  );

  it.each(INHERITED_NAMES)(
    "throws for the notice code %s outside production",
    (code) => {
      const response = makeVisaOracleResponse("SUPPORTED_CANDIDATES");
      response.decision.notices = [{ code, rule_ids: [], source_refs: [] }];
      expect(() => buildEngineOutcome(response)).toThrow();
    },
  );

  it.each(INHERITED_NAMES)(
    "reports the notice code %s as unmapped in production",
    (code) => {
      vi.stubEnv("NODE_ENV", "production");
      const response = makeVisaOracleResponse("SUPPORTED_CANDIDATES");
      response.decision.notices = [{ code, rule_ids: [], source_refs: [] }];
      const outcome = buildEngineOutcome(response);
      expect(outcome.conditions).toEqual([
        { code, message: GENERIC_NOTICE_CONDITION, sourceIds: [] },
      ]);
      expect(emitVisaOracleTelemetry).toHaveBeenCalledWith({
        event: "visa_oracle_v2_notice_unmapped_code",
        code,
      });
    },
  );

  it("still returns the written copy for a mapped notice code", () => {
    const response = makeVisaOracleResponse("SUPPORTED_CANDIDATES");
    response.decision.notices = [
      {
        code: "DISCLOSED_SOURCE_OF_FUNDS_CONDITION",
        rule_ids: [],
        source_refs: [],
      },
    ];
    const outcome = buildEngineOutcome(response);
    expect(outcome.conditions[0].message).toBe(
      NOTICE_CONDITION_COPY.DISCLOSED_SOURCE_OF_FUNDS_CONDITION,
    );
    expect(emitVisaOracleTelemetry).not.toHaveBeenCalled();
  });

  it("still returns the fallback sentence for an ordinary unmapped support code", () => {
    const response = makeVisaOracleResponse();
    response.decision.candidates[0].reason_codes = ["SOMETHING_NEW"];
    const outcome = buildEngineOutcome(response);
    if (outcome.state !== "SUPPORTED_CANDIDATES")
      throw new Error("unexpected state");
    expect(outcome.candidates[0].legal.reasons[0].message).toBe(
      UNMAPPED_REASON_COPY,
    );
  });
});

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const emitVisaOracleTelemetry = vi.hoisted(() => vi.fn());
const nonReversibleHash = vi.hoisted(() =>
  vi.fn(async (value: string) =>
    value.startsWith("{") ? "e".repeat(64) : "i".repeat(64),
  ),
);
vi.mock("../_lib/telemetry", async (importOriginal) => {
  const original = await importOriginal<typeof import("../_lib/telemetry")>();
  return { ...original, emitVisaOracleTelemetry, nonReversibleHash };
});

import {
  createInterviewSnapshot,
  flowReducer,
  initialFlowState,
  type FlowState,
} from "../_lib/flow";
import { findGoldOraclePersona } from "../_lib/gold-oracle-baseline";
import { saveInterviewResume } from "../_lib/resume-store";
import { translate } from "../_lib/i18n";
import { makeVisaOracleResponse } from "../_lib/visa-oracle-test-fixture";
import { OracleShell } from "./OracleShell";

const UNPINNED_ANSWERS = [
  ["in_indonesia", "no"],
  ["holds_stay_permit", "no"],
  ["nationalities", "US"],
  ["birth_date", "1990-01-01"],
  ["category", "tourism"],
  ["trip_scope", "single"],
  ["stay_days", "30"],
  ["entry_pattern", "SINGLE"],
  ["review_gate", "none"],
] as const;

function installVerdictResume(): FlowState {
  let state: FlowState = flowReducer(initialFlowState(), { type: "ADVANCE" });
  for (const [questionId, value] of UNPINNED_ANSWERS) {
    state = flowReducer(state, { type: "ANSWER", questionId, value });
  }
  state = flowReducer(state, { type: "ADVANCE" });
  expect(
    saveInterviewResume(createInterviewSnapshot(state, new Date()), {
      now: new Date(),
    }),
  ).toBe(true);
  return state;
}

function parityEvents(): string[] {
  return emitVisaOracleTelemetry.mock.calls
    .map(([event]) => (event as { event: string }).event)
    .filter((name) => name.startsWith("visa_oracle_v2_parity_"));
}

describe("OracleShell shadow parity telemetry", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    emitVisaOracleTelemetry.mockReset();
    vi.stubEnv("NEXT_PUBLIC_VISA_ORACLE_MODE", "SHADOW");
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("emits neither parity event when the visitor matches no pinned persona", async () => {
    const state = installVerdictResume();
    expect(findGoldOraclePersona(state.facts)).toBeUndefined();
    const response = makeVisaOracleResponse("SUPPORTED_CANDIDATES");
    response.mode = "CURATED";
    global.fetch = vi.fn<typeof fetch>(
      async () =>
        new Response(JSON.stringify(response), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    render(<OracleShell />);

    expect(
      await screen.findByRole("heading", {
        name: translate("en", "verdict.provenance_headline.SHADOW"),
      }),
    ).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledOnce();
    expect(parityEvents()).toEqual([]);
  });
});

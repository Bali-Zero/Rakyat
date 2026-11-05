import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { buildEngineOutcome } from "../_lib/engine-adapter";
import type { Language } from "../_lib/flow";
import type { OracleFacts } from "../_lib/tree";
import { makeVisaOracleResponse } from "../_lib/visa-oracle-test-fixture";
import { OutcomeSheet } from "./OutcomeSheet";

function result(code: string, facts: OracleFacts) {
  const response = makeVisaOracleResponse("SUPPORTED_CANDIDATES");
  response.decision.candidates[0].product_code = code;
  response.display.candidates[0].product_code = code;
  response.decision.notices = [
    {
      code: "DISCLOSED_HEALTH_CONCERN_CONDITION",
      rule_ids: [],
      source_refs: [],
    },
  ];
  return buildEngineOutcome(response, { facts });
}

async function copyResult(
  language: Language,
  outcome: ReturnType<typeof buildEngineOutcome>,
  facts: OracleFacts,
) {
  render(<OutcomeSheet language={language} outcome={outcome} facts={facts} />);
  const copy = vi.mocked(navigator.clipboard.writeText);
  copy.mockClear();
  fireEvent.click(
    screen.getByRole("button", {
      name: language === "id" ? "Salin ringkasan" : "Copy summary",
    }),
  );
  await waitFor(() => expect(copy).toHaveBeenCalledTimes(1));
  return copy.mock.calls[0][0];
}

const cases: [string, OracleFacts, string][] = [
  ["E33F", { retirement_penjamin_confirmed: "no" }, "E33F_PENJAMIN_NOTE"],
  [
    "E31A",
    { family_sponsor_confirmed: "no" },
    "E31A_FAMILY_SPONSOR_NOT_CONFIRMED",
  ],
  [
    "E28A",
    { investment_sponsoring_company_confirmed: "unconfirmed" },
    "E28A_SPONSORING_COMPANY_NOT_CONFIRMED",
  ],
];

describe.each<Language>(["en", "id"])("%s sponsor requirements", (language) => {
  it.each([
    [
      "investment.sponsoring_company_confirmed",
      "investment_sponsoring_company_confirmed",
    ],
    ["family.sponsor_confirmed", "family_sponsor_confirmed"],
  ] as const)(
    "copies the outstanding %s question without sharing unrelated missing inputs",
    async (path, questionId) => {
      const response = makeVisaOracleResponse("NEEDS_INPUT");
      response.decision.missing_facts = [path, "intent.stay_days"];
      const facts = {};
      const outcome = buildEngineOutcome(response, {
        facts,
        editableQuestionIds: [questionId, "stay_days"],
      });
      if (outcome.state !== "NEEDS_INPUT")
        throw new Error("Expected a request for input");
      const summary = await copyResult(language, outcome, facts);
      expect(summary).toContain(outcome.missingInputs[0].message[language]);
      expect(summary).not.toContain(outcome.missingInputs[1].message[language]);
    },
  );
  it.each([
    "E31A_FAMILY_SPONSOR_NOT_CONFIRMED",
    "E28A_SPONSORING_COMPANY_NOT_CONFIRMED",
  ])(
    "copies the sponsor requirement when %s explains a dead end",
    async (code) => {
      const response = makeVisaOracleResponse("NO_SUPPORTED_PATH");
      response.decision.no_path_reasons[0].code = code;
      response.decision.notices = [
        {
          code: "DISCLOSED_HEALTH_CONCERN_CONDITION",
          rule_ids: [],
          source_refs: [],
        },
      ];
      const outcome = buildEngineOutcome(response);
      if (outcome.state !== "NO_SUPPORTED_PATH")
        throw new Error("Expected a dead end");
      const requirement = outcome.noPathReasons[0].message[language];
      const summary = await copyResult(language, outcome, {});
      expect(screen.getByText(requirement)).toBeInTheDocument();
      expect(summary).toContain(requirement);
      expect(summary).not.toContain(outcome.conditions[0].message[language]);
    },
  );
  it.each(cases)(
    "preserves the %s sponsor requirement when copying the result",
    async (code, facts, reasonCode) => {
      const outcome = result(code, facts);
      const note = outcome.conditions.find((item) => item.code === reasonCode);
      expect(note).toBeDefined();
      const summary = await copyResult(language, outcome, facts);
      expect(screen.getByText(note!.message[language])).toBeInTheDocument();
      expect(summary).toContain(note!.message[language]);
      expect(summary).not.toContain(outcome.conditions[0].message[language]);
    },
  );
});

it.each<[string, OracleFacts, boolean]>([
  ["E31A", { family_spouse_application_support_confirmed: "yes" }, false],
  ["E31A", { family_sponsor_confirmed: "yes" }, true],
  ["E31A", {}, true],
  [
    "E28A",
    {
      sponsor_category: "INVESTMENT",
      investment_sponsoring_company_confirmed: "yes",
    },
    false,
  ],
  [
    "E28A",
    {
      sponsor_category: "NONE",
      investment_sponsoring_company_confirmed: "yes",
    },
    true,
  ],
  ["E28A", { family_sponsor_confirmed: "yes" }, true],
  ["E28B", { sponsor_category: "NONE" }, false],
])(
  "only qualifies a %s recommendation when its own sponsor is unconfirmed",
  (code, facts, expected) => {
    const notes = result(code, facts).conditions.filter(
      (item) => item.code !== "DISCLOSED_HEALTH_CONCERN_CONDITION",
    );
    expect(notes).toHaveLength(expected ? 1 : 0);
  },
);

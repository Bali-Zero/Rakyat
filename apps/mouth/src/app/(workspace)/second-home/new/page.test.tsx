import React from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  E33E_LIVE_PRICE_KEY,
  E33F_OFFSHORE_LIVE_PRICE_KEY,
  E33_LIVE_PRICE_CATEGORY,
  E33_LIVE_PRICE_KEY,
} from "@/lib/secondhome-studio/pricing-key";
import { getPricingSnapshotEntry } from "@/lib/pricing-snapshot";
import {
  HISTORICAL_PRICE_RE,
  PRICE_LITERAL_RE,
  visibleSourceText,
} from "@/test/visible-source-text";
import NewSecondHomeCasePage from "./page";

const { mockMutateAsync, mockGetProfile, mockPush } = vi.hoisted(() => ({
  mockMutateAsync: vi.fn(),
  mockGetProfile: vi.fn(),
  mockPush: vi.fn(),
}));

const catalogue = JSON.parse(
  readFileSync(
    join(
      __dirname,
      "..",
      "..",
      "..",
      "..",
      "..",
      "data",
      "bali-zero-prices.json",
    ),
    "utf8",
  ),
) as {
  services_by_category: Record<string, Record<string, Record<string, unknown>>>;
};
const catalogueEntries =
  catalogue.services_by_category[E33_LIVE_PRICE_CATEGORY];
const e33Catalogue = catalogueEntries[E33_LIVE_PRICE_KEY];
const e33eCatalogue = catalogueEntries[E33E_LIVE_PRICE_KEY];
const e33fCatalogue = catalogueEntries[E33F_OFFSHORE_LIVE_PRICE_KEY];
const e33Entry = getPricingSnapshotEntry(
  E33_LIVE_PRICE_CATEGORY,
  E33_LIVE_PRICE_KEY,
);
const e33eEntry = getPricingSnapshotEntry(
  E33_LIVE_PRICE_CATEGORY,
  E33E_LIVE_PRICE_KEY,
);
const e33fEntry = getPricingSnapshotEntry(
  E33_LIVE_PRICE_CATEGORY,
  E33F_OFFSHORE_LIVE_PRICE_KEY,
);

/** The expected value, asserted non-empty first so a catalogue change cannot pass vacuously. */
const expectedText = (value: unknown): string => {
  expect(typeof value).toBe("string");
  expect(String(value).trim()).not.toBe("");
  return String(value);
};

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));
vi.mock("@/lib/api", () => ({
  api: {
    getProfile: mockGetProfile,
    crm: { getClientPractices: vi.fn().mockResolvedValue([]) },
  },
}));
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/hooks/useSecondHome", () => ({
  useCreateSecondHomeCase: () => ({
    mutateAsync: mockMutateAsync,
    isPending: false,
  }),
}));
vi.mock("../components/ClientPicker", () => ({
  ClientPicker: ({
    onSelect,
  }: {
    onSelect: (c: { id: number; full_name: string }) => void;
  }) => (
    <button
      type="button"
      onClick={() => onSelect({ id: 7, full_name: "Synthetic Client" })}
    >
      pick-client
    </button>
  ),
}));

describe("new Second Home case page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetProfile.mockResolvedValue({});
    mockMutateAsync.mockResolvedValue({ case_id: "E33-2026-synthetic" });
  });

  it("states the catalogue facts for E33F: income or allowance, USD 3,000, 1 year, sponsor", () => {
    render(<NewSecondHomeCasePage />);
    const panel = screen.getByTestId("e33-facts-panel").textContent ?? "";
    expect(panel).toContain(expectedText(e33fCatalogue.description_en));
    expect(panel).toContain(expectedText(e33Catalogue.validity));
    expect(panel).toContain(expectedText(e33eCatalogue.validity));
    expect(panel).toContain(expectedText(e33Catalogue.price));
    expect(panel).toContain(expectedText(e33eCatalogue.price));
    expect(panel).not.toMatch(/passive\s+income/i);
    // prices come from the catalogue snapshot, in its own format
    expect(panel).toMatch(/\d{2}\.\d{3}\.\d{3} IDR/);
  });

  it.each([
    ["missing", undefined],
    ["null", null],
    ["empty", ""],
    ["whitespace-only", "   "],
  ])(
    "shows the E33F catalogue-description unavailable state when description_en is %s",
    (_caseName, description) => {
      const entry = e33fEntry as unknown as Record<string, unknown>;
      const had = Object.prototype.hasOwnProperty.call(entry, "description_en");
      const original = entry.description_en;
      try {
        entry.description_en = description;
        render(<NewSecondHomeCasePage />);
        const panel = screen.getByTestId("e33-facts-panel");
        expect(panel).toHaveTextContent(
          "Catalogue description unavailable — check the E33F catalogue entry before opening this case.",
        );
        expect(panel).not.toHaveTextContent(
          "The income-only route needs a senior-income declaration.",
        );
      } finally {
        if (had) entry.description_en = original;
        else delete entry.description_en;
      }
    },
  );

  it("renders a present E33F catalogue description verbatim", () => {
    render(<NewSecondHomeCasePage />);
    expect(screen.getByTestId("e33-facts-panel")).toHaveTextContent(
      expectedText(e33fCatalogue.description_en),
    );
  });

  it.each([
    ["E33", e33Entry, e33Catalogue, e33eCatalogue],
    ["E33E", e33eEntry, e33eCatalogue, e33Catalogue],
  ])(
    "shows validity unavailable for %s only when validity is absent",
    (_name, liveEntry, expected, otherExpected) => {
      expectedText(expected.validity);
      const otherValidity = expectedText(otherExpected.validity);
      const entry = liveEntry as unknown as Record<string, unknown>;
      const had = Object.prototype.hasOwnProperty.call(entry, "validity");
      const original = entry.validity;
      for (const value of [undefined, null, "", "   "]) {
        try {
          if (value === undefined) delete entry.validity;
          else entry.validity = value;
          cleanup();
          render(<NewSecondHomeCasePage />);
          const panel = screen.getByTestId("e33-facts-panel");
          expect(panel.getElementsByTagName("p")).toHaveLength(4);
          expect(
            panel.textContent?.match(/catalogue validity unavailable/g),
          ).toHaveLength(1);
          expect(panel).toHaveTextContent(otherValidity);
        } finally {
          if (had) entry.validity = original;
          else delete entry.validity;
        }
      }
    },
  );

  it("shows validity unavailable when the E33 catalogue entry is absent", async () => {
    vi.resetModules();
    vi.doMock("@/lib/pricing-snapshot", async () => {
      const actual = await vi.importActual<
        typeof import("@/lib/pricing-snapshot")
      >("@/lib/pricing-snapshot");
      return {
        ...actual,
        getPricingSnapshotEntry: (category: string, key: string) =>
          category === E33_LIVE_PRICE_CATEGORY && key === E33_LIVE_PRICE_KEY
            ? undefined
            : actual.getPricingSnapshotEntry(category, key),
      };
    });
    try {
      const { default: FreshPage } = await import("./page");
      render(<FreshPage />);
      const panel = screen.getByTestId("e33-facts-panel");
      expect(
        panel.textContent?.match(/catalogue validity unavailable/g),
      ).toHaveLength(1);
      expect(panel).toHaveTextContent(expectedText(e33eCatalogue.validity));
    } finally {
      vi.doUnmock("@/lib/pricing-snapshot");
      vi.resetModules();
    }
  });

  it("still states the base E33 / E33E facts staff rely on", () => {
    render(<NewSecondHomeCasePage />);
    const panel = screen.getByTestId("e33-facts-panel").textContent ?? "";
    for (const fact of [
      "strata-title",
      "No sponsor",
      expectedText(e33Catalogue.validity),
      "Pasal 113",
      "55–59",
      "BUMN",
      "USD 130,000",
      "USD 1,000,000",
    ]) {
      expect(panel).toContain(fact);
    }
  });

  it("the page source carries no price literal or passive-income phrase", () => {
    const src = readFileSync(join(__dirname, "page.tsx"), "utf8");
    const text = visibleSourceText(src, "page.tsx");
    expect(text.some((value) => PRICE_LITERAL_RE.test(value))).toBe(false);
    expect(text.some((value) => HISTORICAL_PRICE_RE.test(value))).toBe(false);
    expect(text.some((value) => /passive\s+income/i.test(value))).toBe(false);
    expect(
      visibleSourceText("<p>35.000.000 IDR</p>").some((value) =>
        PRICE_LITERAL_RE.test(value),
      ),
    ).toBe(true);
  });

  it("posts no owner_email when the owner is blank", async () => {
    const user = userEvent.setup();
    render(<NewSecondHomeCasePage />);
    await user.click(screen.getByText("pick-client"));
    await user.click(screen.getByRole("button", { name: /create case/i }));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    const body = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;
    expect(JSON.stringify(body)).not.toContain("owner_email");
    expect(body.client_id).toBe(7);
  });

  it("posts the owner_email when one is typed", async () => {
    const user = userEvent.setup();
    render(<NewSecondHomeCasePage />);
    await user.click(screen.getByText("pick-client"));
    await user.type(
      screen.getByPlaceholderText("team-member@balizero.com"),
      "owner@example.test",
    );
    await user.click(screen.getByRole("button", { name: /create case/i }));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    expect(mockMutateAsync.mock.calls[0][0]).toMatchObject({
      owner_email: "owner@example.test",
    });
  });
});

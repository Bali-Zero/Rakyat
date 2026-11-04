import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { buildEngineOutcome } from "../_lib/engine-adapter";
import type { Language } from "../_lib/flow";
import { makeVisaOracleResponse } from "../_lib/visa-oracle-test-fixture";
import { OutcomeSheet } from "./OutcomeSheet";

const option = (days: number, amount: number | null, selected: boolean) => ({
  days,
  amount_idr: amount,
  selected,
  status: "AVAILABLE",
  reason_code: "DURATION_OPTION",
  pricing_key: {
    category: "visa",
    item_key: days === 365 ? "E31A_1Y" : "E31A_2Y",
  },
});

function outcomeWith(
  extra: Record<string, unknown>,
  quoted = true,
  facts: Record<string, string> = {},
) {
  const response = makeVisaOracleResponse("SUPPORTED_CANDIDATES");
  const candidate = response.display.candidates[0];
  Object.assign(candidate, extra);
  candidate.pricing = {
    status: "AVAILABLE",
    reason_code: "PRICE_AVAILABLE",
    evaluated_at: "2026-08-03T04:00:00Z",
    catalog_last_updated: "2026-08-03",
    catalog_sha256: "b".repeat(64),
    row_sha256: "c".repeat(64),
  };
  const chosen = (
    (extra.duration_options as ReturnType<typeof option>[] | undefined) ?? []
  ).find((item) => item.selected);
  response.decision.quotes = [
    {
      quote_id: "55555555-5555-4555-8555-555555555555",
      product_version_id: candidate.product_version_id,
      product_code: candidate.product_code,
      status: "AVAILABLE",
      currency: "IDR",
      amount: chosen?.amount_idr ?? 15_000_000,
      pricing_key: chosen?.pricing_key ?? { category: "visa", item_key: "C1" },
      catalog_version: "2026.08",
      catalog_sha256: "b".repeat(64),
      row_sha256: "c".repeat(64),
      quoted_at: "2026-08-03T04:00:00Z",
      valid_until: null,
      reason_code: "PRICE_AVAILABLE",
    },
  ];
  if (!quoted) response.decision.quotes = [];
  return buildEngineOutcome(response, { facts });
}

function priceText(
  extra: Record<string, unknown>,
  language: Language = "en",
  facts: Record<string, string> = {},
): string {
  const { container } = render(
    <OutcomeSheet
      language={language}
      outcome={outcomeWith(extra, true, facts)}
      facts={{}}
    />,
  );
  return container.querySelector(".oracle-price")?.textContent ?? "";
}

async function copiedSummary(
  extra: Record<string, unknown>,
  language: Language = "en",
  quoted = true,
): Promise<string> {
  render(
    <OutcomeSheet
      language={language}
      outcome={outcomeWith(extra, quoted)}
      facts={{}}
    />,
  );
  const writeText = navigator.clipboard.writeText as ReturnType<typeof vi.fn>;
  writeText.mockClear();
  fireEvent.click(
    screen.getByRole("button", {
      name: language === "id" ? "Salin ringkasan" : "Copy summary",
    }),
  );
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  return writeText.mock.calls[0]?.[0] as string;
}

describe("OutcomeSheet — stay-permit duration under the price", () => {
  it("365 selected: names the 1-year permit and offers the 2-year one", () => {
    const text = priceText({
      selected_duration_days: 365,
      duration_options: [
        option(365, 11_000_000, true),
        option(730, 15_000_000, false),
      ],
      extension_required: false,
    });
    expect(text).toContain("1-year stay permit");
    expect(text).toMatch(/Also available: 2 years — .*15,000,000/);
    expect(text).not.toContain("longer than the longest permit");
  });

  it("730 selected: names the 2-year permit and offers the 1-year one", () => {
    const text = priceText({
      selected_duration_days: 730,
      duration_options: [
        option(365, 11_000_000, false),
        option(730, 15_000_000, true),
      ],
    });
    expect(text).toContain("2-year stay permit");
    expect(text).toMatch(/Also available: 1 year — .*11,000,000/);
  });

  it("a stay beyond the longest permit adds the renewal line", () => {
    const text = priceText({
      selected_duration_days: 730,
      duration_options: [
        option(365, 11_000_000, false),
        option(730, 15_000_000, true),
      ],
      extension_required: true,
    });
    expect(text).toContain("ask your advisor about renewal");
  });

  it("reads in Indonesian", () => {
    const text = priceText(
      {
        selected_duration_days: 730,
        duration_options: [
          option(365, 11_000_000, false),
          option(730, 15_000_000, true),
        ],
      },
      "id",
    );
    expect(text).toContain("Izin tinggal 2 tahun");
    expect(text).toMatch(/Pilihan lain: 1 tahun — /);
  });

  it("lists no option without an amount, and counts non-year permits in days", () => {
    const text = priceText({
      selected_duration_days: 180,
      duration_options: [
        option(180, 5_000_000, true),
        option(365, null, false),
      ],
    });
    expect(text).toContain("180-day stay permit");
    expect(text).not.toContain("Also available");
  });

  it("C1: a fixed 60-day stay_policy renders the 60-day label and the extension line (EN + ID)", () => {
    const en = priceText({});
    expect(en).toContain("60-day stay permit");
    const id = priceText({}, "id");
    expect(id).toContain("Izin tinggal 60 hari");
  });

  it("C1 keeps the 180-day total line; D1/D2 show the per-entry line; D12 shows neither", () => {
    const c1 = priceText({});
    expect(c1).toContain(
      "This stay can be extended up to 2 times, 60 days each, for up to 180 days in total.",
    );
    for (const product_code of ["D1", "D2"]) {
      const en = priceText({ product_code });
      expect(en).toContain("60-day stay permit");
      expect(en).toContain(
        "Each entry allows a 60-day stay, extendable up to 2 times by 60 days.",
      );
      expect(en).not.toContain("180 days in total");
      const id = priceText({ product_code }, "id");
      expect(id).toContain("Izin tinggal 60 hari");
      expect(id).toContain("Setiap kedatangan memberi izin tinggal 60 hari");
    }
    const d12 = priceText({ product_code: "D12" });
    expect(d12).toContain("60-day stay permit");
    expect(d12).not.toContain("Each entry allows");
    expect(d12).not.toContain("180 days in total");
  });

  it("a product without duration renders the price block exactly as before", () => {
    const text = priceText({
      stay_policy: {
        stay: {
          kind: "VARIABLE_BY_GRANT" as const,
          minimum_days: null,
          maximum_days: null,
        },
        extension: {
          status: "UNKNOWN" as const,
          reason_code: null,
          allowed: false,
          maximum_extensions: 0,
          days_per_extension: null,
        },
      },
    });
    expect(text).not.toMatch(/stay permit|Also available/);
    expect(text).toMatch(/Government fees and Bali Zero service included\.$/);
  });

  it("share summary: a candidate with a duration carries price and permit after its name", async () => {
    const summary = await copiedSummary({
      selected_duration_days: 730,
      duration_options: [
        option(365, 11_000_000, false),
        option(730, 15_000_000, true),
      ],
    });
    expect(summary).toMatch(
      /: IDR\s15,000,000 · Government fees and Bali Zero service included\. · 2-year stay permit$/m,
    );
  });

  it("share summary: a priced candidate without a duration still carries its price", async () => {
    const summary = await copiedSummary({
      stay_policy: {
        stay: {
          kind: "VARIABLE_BY_GRANT" as const,
          minimum_days: null,
          maximum_days: null,
        },
        extension: {
          status: "UNKNOWN" as const,
          reason_code: null,
          allowed: false,
          maximum_extensions: 0,
          days_per_extension: null,
        },
      },
    });
    expect(summary.replace(/[\u00a0\u202f]/g, " ")).toMatch(
      /^[A-Z0-9]+ — .+: IDR 15,000,000 · Government fees and Bali Zero service included\.$/m,
    );
    expect(summary).not.toMatch(/stay permit/);
  });

  it("share summary: a candidate with neither price nor duration is just its name", async () => {
    const summary = await copiedSummary({}, "en", false);
    expect(summary).not.toMatch(/stay permit|·|IDR/);
    expect(summary).toMatch(/^[A-Z0-9]+ — [^:]+: [^·]+$/m);
  });

  it("share summary reads in Indonesian", async () => {
    const summary = await copiedSummary(
      {
        selected_duration_days: 365,
        duration_options: [
          option(365, 11_000_000, true),
          option(730, 15_000_000, false),
        ],
      },
      "id",
    );
    expect(summary).toMatch(/ · Izin tinggal 1 tahun$/m);
  });

  // F6 (2026-10-15): E28A is a pack FIXED_DAYS 730..730 product with no
  // duration options; its card names the 2-year permit (sold as 2 years only)
  // and, for a plan beyond 730 days, the longer-stay renewal line (the E28A
  // page states the permit is extendable).
  describe("E28A: a fixed 730-day stay_policy names the 2-year permit, with the renewal line for a longer plan", () => {
    const e28a = {
      stay_policy: {
        stay: { kind: "FIXED_DAYS", minimum_days: 730, maximum_days: 730 },
        extension: {
          status: "VERIFIED",
          reason_code: null,
          allowed: true,
          maximum_extensions: 100,
          days_per_extension: 365,
        },
      },
    };

    // stay_days > 730 exercises the longer-stay branch: the E28A page states the
    // permit is extendable ("dapat diperpanjang"), so the renewal line is TRUE.
    it("EN: '2-year stay permit', and a 1000-day plan shows the continue-by-renewal line", () => {
      const text = priceText(e28a, "en", { stay_days: "1000" });
      expect(text).toContain("2-year stay permit");
      expect(text).toContain("your stay can continue by renewal");
      expect(text).not.toMatch(/Also available/);
    });

    it("ID: 'Izin tinggal 2 tahun', and a 1000-day plan shows the continue-by-renewal line", () => {
      const text = priceText(e28a, "id", { stay_days: "1000" });
      expect(text).toContain("Izin tinggal 2 tahun");
      expect(text).toContain("dapat dilanjutkan dengan perpanjangan");
      expect(text).not.toMatch(/Pilihan lain/);
    });

    it("a plan within the 2 years shows the length and no renewal line", () => {
      const text = priceText(e28a, "en", { stay_days: "700" });
      expect(text).toContain("2-year stay permit");
      expect(text).not.toMatch(/renewal/);
    });

    it("a fixed 1095-day stay with no mapped length renders as a 3-year permit", () => {
      const text = priceText({
        stay_policy: {
          ...e28a.stay_policy,
          stay: { kind: "FIXED_DAYS", minimum_days: 1095, maximum_days: 1095 },
        },
      });
      expect(text).toContain("3-year stay permit");
    });
  });
});

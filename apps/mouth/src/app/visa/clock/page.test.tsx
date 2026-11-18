import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import VisaClockPage from "./page";

/**
 * W0b telemetry regression guard (2026-07-23): same silent-death pattern
 * as visa/match — submit failure was swallowed into `setError` with no
 * event. Failure telemetry carries endpoint + status only, never the
 * visa type / entry date (Law 2).
 */

const trackerMocks = vi.hoisted(() => ({
  formStarted: vi.fn(),
  formSubmitted: vi.fn(),
  formSubmitFailed: vi.fn(),
}));

vi.mock("@balizero/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@balizero/core")>();
  return {
    ...actual,
    useFunnelApp: () => ({
      viewed: vi.fn(),
      formStarted: trackerMocks.formStarted,
      formSubmitted: trackerMocks.formSubmitted,
      formSubmitFailed: trackerMocks.formSubmitFailed,
    }),
  };
});

const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;

function fillAndSubmit() {
  fireEvent.change(screen.getByLabelText("Entry date"), {
    target: { value: "2026-07-01" },
  });
  fireEvent.change(screen.getByLabelText("Visa type"), {
    target: { value: "B1" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Show my timeline" }));
}

describe("VisaClockPage — submit telemetry (W0b)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchMock.mockReset();
  });

  it("HTTP error fires app_form_submit_failed with status, payload-free", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 503 });
    render(<VisaClockPage />);
    fillAndSubmit();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        /could not build your timeline/i,
      ),
    );
    expect(trackerMocks.formSubmitted).toHaveBeenCalledWith([
      "visa_type",
      "entry_date",
    ]);
    expect(trackerMocks.formSubmitFailed).toHaveBeenCalledTimes(1);
    expect(trackerMocks.formSubmitFailed).toHaveBeenCalledWith(
      "/api/visa/clock",
      503,
    );
    // Law 2: no form values in the failure event.
    const emitted = JSON.stringify(trackerMocks.formSubmitFailed.mock.calls);
    expect(emitted).not.toContain("2026-07-01");
    expect(emitted).not.toContain("B1");
  });

  it("network failure (fetch rejects) fires the event with status null", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    render(<VisaClockPage />);
    fillAndSubmit();

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(trackerMocks.formSubmitFailed).toHaveBeenCalledWith(
      "/api/visa/clock",
      null,
    );
  });

  it("2xx with an unparseable body reports the HTTP status, not null", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError("Unexpected token");
      },
    });
    render(<VisaClockPage />);
    fillAndSubmit();

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(trackerMocks.formSubmitFailed).toHaveBeenCalledWith(
      "/api/visa/clock",
      200,
    );
  });

  it("successful submit fires no failure event", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ hash: "xyz789" }),
    });
    render(<VisaClockPage />);
    fillAndSubmit();

    await waitFor(() => expect(trackerMocks.formSubmitted).toHaveBeenCalled());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(trackerMocks.formSubmitFailed).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("VisaClockPage — backend catalogue parity (no retired B211A 422)", () => {
  it("excludes retired B211A and offers only valid backend VisaType codes with accurate C7 labels", () => {
    const cataloguePath = join(
      __dirname,
      "../../../../../backend-rag/backend/services/visa_check/catalogue.py",
    );
    const catalogueSrc = readFileSync(cataloguePath, "utf-8");
    const enumBlock =
      catalogueSrc
        .split("class VisaType(str, Enum):")[1]
        ?.split("@dataclass")[0] ?? "";
    const backendVisaTypes = new Set(
      [...enumBlock.matchAll(/=\s*"([^"]+)"/g)].map((m) => m[1]),
    );
    expect(backendVisaTypes.size).toBeGreaterThanOrEqual(15);

    render(<VisaClockPage />);
    const select = screen.getByLabelText("Visa type") as HTMLSelectElement;
    const options = Array.from(select.options).filter((o) => o.value !== "");

    expect(options.length).toBeGreaterThan(0);
    const codes = options.map((o) => o.value);
    expect(codes).not.toContain("B211A");
    expect(codes).toContain("C1");
    for (const code of codes) {
      expect(
        backendVisaTypes.has(code),
        `${code} is not a valid VisaType in catalogue.py`,
      ).toBe(true);
    }

    const labelByCode = Object.fromEntries(
      options.map((o) => [o.value, o.textContent ?? ""]),
    );
    expect(labelByCode.C7).toMatch(/Art & Culture Performance/i);
    expect(labelByCode.C7).not.toMatch(/Job training/i);
    expect(labelByCode.C7A).toMatch(/Music Performance/i);
    expect(labelByCode.C7B).toMatch(/Music Crew/i);
    expect(labelByCode.C7B).not.toMatch(/Sport/i);
  });
});

describe("VisaClockPage — Not sure hands off to the visa team", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchMock.mockReset();
  });

  it("is a separate button, never an option of the Visa type select", () => {
    render(<VisaClockPage />);
    const select = screen.getByLabelText("Visa type") as HTMLSelectElement;
    expect(
      Array.from(select.options).some((o) =>
        /not sure/i.test(o.textContent ?? ""),
      ),
    ).toBe(false);
    const button = screen.getByRole("button", {
      name: "Not sure which visa you are on? Ask us",
    });
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("clicking it shows the existing WhatsApp CTA to the visa funnel", () => {
    render(<VisaClockPage />);
    expect(
      screen.queryByRole("link", { name: /ask the visa team on whatsapp/i }),
    ).not.toBeInTheDocument();

    const button = screen.getByRole("button", { name: /not sure which visa/i });
    fireEvent.click(button);

    expect(button).toHaveAttribute("aria-expanded", "true");
    const link = screen.getByRole("link", {
      name: /ask the visa team on whatsapp/i,
    });
    const href = new URL(link.getAttribute("href") ?? "");
    expect(href.hostname).toBe("wa.me");
    expect(href.searchParams.get("utm_campaign")).toBe("visa");
    expect(href.searchParams.get("utm_medium")).toBe("whatsapp_cta");
  });

  it("never posts to /api/visa/clock and keeps the count at real visa types", () => {
    render(<VisaClockPage />);
    fireEvent.click(
      screen.getByRole("button", { name: /not sure which visa/i }),
    );

    expect(fetchMock).not.toHaveBeenCalled();
    expect(trackerMocks.formSubmitted).not.toHaveBeenCalled();
    // Trust strip counts visa types only (13), not the ask-us button.
    expect(screen.getByText("13")).toBeInTheDocument();
  });
});

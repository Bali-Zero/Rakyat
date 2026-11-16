import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import {
  DEFAULT_MOBILE_SEARCH_PLACEHOLDER,
  DEFAULT_SEARCH_PLACEHOLDER,
  KBLISearch,
  formatPmaChipLabel,
} from "./KBLISearch";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const search = vi.fn();
vi.mock("@/lib/api/kbli.api", () => ({
  kbliApi: { search: (q: string) => search(q) },
  apiPmaStatusLabel: (r: { pma_status?: string }) => r.pma_status ?? "TERBUKA",
  isApiPmaVerdictVerified: () => true,
}));
vi.mock("@/lib/analytics", () => ({ trackKBLISearch: vi.fn() }));

function stubMatchMedia(initialMobile: boolean) {
  let matches = initialMobile;
  const listeners = new Set<() => void>();

  const matchMediaMock = vi.fn().mockImplementation((query: string) => ({
    get matches() {
      return matches;
    },
    media: query,
    onchange: null,
    addEventListener: (_event: string, cb: () => void) => {
      listeners.add(cb);
    },
    removeEventListener: (_event: string, cb: () => void) => {
      listeners.delete(cb);
    },
    addListener: (cb: () => void) => {
      listeners.add(cb);
    },
    removeListener: (cb: () => void) => {
      listeners.delete(cb);
    },
    dispatchEvent: () => true,
  }));

  vi.stubGlobal("matchMedia", matchMediaMock);

  return {
    setMobile(next: boolean) {
      matches = next;
      listeners.forEach((cb) => cb());
    },
  };
}

describe("KBLISearch <768px mobile placeholder & EN result chips", () => {
  beforeEach(() => {
    push.mockReset();
    search.mockReset();
    search.mockResolvedValue([
      {
        code: "56101",
        title: "Restaurants",
        description: "Food service activities",
        pma_status: "TERBUKA",
        risk_category: "Menengah Rendah",
      },
    ]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses the concise mobile placeholder (<768px) so a 390px phone never clips 451px of copy, while keeping full aria-label", () => {
    stubMatchMedia(true);
    render(<KBLISearch />);

    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.placeholder).toBe(DEFAULT_MOBILE_SEARCH_PLACEHOLDER);
    expect(input.placeholder.length).toBeLessThanOrEqual(24);
    expect(input).toHaveAttribute("aria-label", DEFAULT_SEARCH_PLACEHOLDER);

    // Responsive sizing: 16px (`text-base`) on <768px (prevents iOS auto-zoom, >=11px floor) and `md:text-lg` on desktop
    expect(input.className).toContain("text-base");
    expect(input.className).toContain("md:text-lg");
    expect(input.className).toContain("pl-11");
    expect(input.className).toContain("md:pl-12");
    expect(input.className).toContain("pr-4");
    expect(input.className).toContain("md:pr-10");
  });

  it("keeps the full 58-char placeholder on >=768px desktop viewports and updates live across breakpoint resize", () => {
    const viewport = stubMatchMedia(false);
    render(<KBLISearch />);

    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.placeholder).toBe(DEFAULT_SEARCH_PLACEHOLDER);

    act(() => {
      viewport.setMobile(true);
    });
    expect(input.placeholder).toBe(DEFAULT_MOBILE_SEARCH_PLACEHOLDER);
    expect(input).toHaveAttribute("aria-label", DEFAULT_SEARCH_PLACEHOLDER);

    act(() => {
      viewport.setMobile(false);
    });
    expect(input.placeholder).toBe(DEFAULT_SEARCH_PLACEHOLDER);
  });

  it("switches right padding to pr-10 once query is non-empty so text clears the X clear button", () => {
    stubMatchMedia(true);
    render(<KBLISearch />);

    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.className).toContain("pr-4");

    fireEvent.change(input, { target: { value: "villa" } });
    expect(input.className).toContain("pr-10");
    expect(input.className).not.toContain("pr-4");
  });

  it("respects custom placeholder and mobilePlaceholder props", () => {
    stubMatchMedia(true);
    const { rerender } = render(<KBLISearch placeholder="Find KBLI" />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.placeholder).toBe("Find KBLI");

    rerender(
      <KBLISearch
        placeholder="Find KBLI by code, keyword, or sector"
        mobilePlaceholder="Find KBLI..."
      />,
    );
    expect(input.placeholder).toBe("Find KBLI...");
  });

  it("renders English PMA and risk labels in the dropdown instead of raw Indonesian tokens", async () => {
    stubMatchMedia(true);
    render(<KBLISearch />);

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "rest" } });

    const option = await screen.findByRole("option", {}, { timeout: 2000 });
    expect(option).toHaveTextContent("Open to Foreigners");
    expect(option).toHaveTextContent("Medium-Low");
    expect(option).not.toHaveTextContent("TERBUKA");
    expect(option).not.toHaveTextContent("Menengah Rendah");

    expect(formatPmaChipLabel("TERBATAS · ownership cap not verified")).toBe(
      "Restricted · ownership cap not verified",
    );
    expect(formatPmaChipLabel("TERTUTUP")).toBe("Closed to Foreigners");
    expect(formatPmaChipLabel("PMA not verified")).toBe("PMA not verified");
  });
});

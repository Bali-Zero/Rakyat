import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { KBLISearch } from "./KBLISearch";

/**
 * KBLI Navigator on a phone:
 *   - every tap target in the search is at least 44px (min-h-11 / min-w-11);
 *   - the results list is capped to what is visible above the on-screen
 *     keyboard (window.visualViewport), not a fixed 400px that runs under it.
 * The search bar's stickiness lives in app/kbli/page.tsx and is unchanged.
 */

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
const search = vi.fn();
vi.mock("@/lib/api/kbli.api", () => ({
  kbliApi: { search: (q: string) => search(q) },
  apiPmaStatusLabel: () => "Open",
  isApiPmaVerdictVerified: () => true,
}));
vi.mock("@/lib/analytics", () => ({ trackKBLISearch: vi.fn() }));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));

const result = (code: string) => ({
  code,
  title_en: `Synthetic activity ${code}`,
  title_id: `Kegiatan ${code}`,
  pma_status: "open",
});

afterEach(() => {
  Reflect.deleteProperty(window, "visualViewport");
  search.mockReset();
});

describe("KBLISearch on mobile", () => {
  it("quick filters and the clear button are 44px tap targets", () => {
    render(<KBLISearch quickFilters={["Restaurant"]} />);
    expect(
      screen.getByRole("button", { name: "Search Restaurant" }).className,
    ).toMatch(/\bmin-h-11\b/);

    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "villa" },
    });
    const clear = screen.getByRole("button", { name: "Clear search" });
    expect(clear.className).toMatch(/\bmin-h-11\b/);
    expect(clear.className).toMatch(/\bmin-w-11\b/);
  });

  it("caps the results list above the on-screen keyboard", async () => {
    const listeners: Record<string, () => void> = {};
    Object.defineProperty(window, "visualViewport", {
      configurable: true,
      value: {
        height: 300, // keyboard open: 300px left visible
        offsetTop: 0,
        addEventListener: (t: string, cb: () => void) => (listeners[t] = cb),
        removeEventListener: vi.fn(),
      },
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      top: 100,
    } as DOMRect);
    search.mockResolvedValue([result("62019"), result("62020")]);

    render(<KBLISearch />);
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "software" },
    });
    const list = await screen.findByRole("listbox", undefined, {
      timeout: 2000,
    });
    await act(async () => {
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    // 300 visible - 100 list top - 8 gap
    expect(list.style.maxHeight).toBe("192px");
  });

  it("innocence: without visualViewport the list keeps its 400px cap", async () => {
    Object.defineProperty(window, "visualViewport", {
      configurable: true,
      value: undefined,
    });
    search.mockResolvedValue([result("62019")]);
    render(<KBLISearch />);
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "software" },
    });
    const list = await screen.findByRole("listbox", undefined, {
      timeout: 2000,
    });
    expect(list.style.maxHeight).toBe("");
    expect(list.className).toMatch(/max-h-\[400px\]/);
  });
});

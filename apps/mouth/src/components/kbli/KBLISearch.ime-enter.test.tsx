import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { KBLISearch } from "./KBLISearch";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
const search = vi.fn();
vi.mock("@/lib/api/kbli.api", () => ({
  kbliApi: { search: (q: string) => search(q) },
  apiPmaStatusLabel: () => "Open",
  isApiPmaVerdictVerified: () => true,
}));
vi.mock("@/lib/analytics", () => ({ trackKBLISearch: vi.fn() }));

/**
 * A Japanese, Chinese or Korean visitor confirms IME candidates with Enter.
 * Before 2026-11-01 that Enter navigated to /kbli?q=<half-typed word>.
 */
describe("KBLISearch Enter while an IME composes", () => {
  beforeEach(() => {
    push.mockReset();
    search.mockReset();
    search.mockResolvedValue([]);
  });

  it("ignores the Enter that commits a candidate (isComposing)", () => {
    render(<KBLISearch />);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "レストラン" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(push).not.toHaveBeenCalled();
  });

  it("ignores Safari's keyCode 229 Enter", () => {
    render(<KBLISearch />);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "餐厅" } });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(push).not.toHaveBeenCalled();
  });

  it("still navigates on a plain Enter", () => {
    render(<KBLISearch />);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "restaurant" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(push).toHaveBeenCalledWith("/kbli?q=restaurant");
  });

  it("keeps mobile keyboards from rewriting KITAS, LKPM, BPJS", () => {
    render(<KBLISearch />);
    const input = screen.getByRole("combobox");
    expect(input.getAttribute("autocorrect")).toBe("off");
    expect(input.getAttribute("autocapitalize")).toBe("off");
    expect(input.getAttribute("spellcheck")).toBe("false");
  });
});

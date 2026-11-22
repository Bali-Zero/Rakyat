import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ConsentBanner } from "./ConsentBanner";

const DISMISS = /got it/i;

describe("ConsentBanner with unavailable storage", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("guilt: a read that throws still mounts and shows the banner", () => {
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("storage blocked");
    });

    expect(() => render(<ConsentBanner />)).not.toThrow();
    expect(screen.getByRole("button", { name: DISMISS })).toBeInTheDocument();
  });

  it("guilt: a write that throws still hides the banner on dismiss", () => {
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("storage blocked");
    });

    render(<ConsentBanner />);
    fireEvent.click(screen.getByRole("button", { name: DISMISS }));

    expect(screen.queryByRole("button", { name: DISMISS })).toBeNull();
  });

  it("innocence: with working storage a dismissed banner stays hidden on remount", () => {
    const first = render(<ConsentBanner />);
    fireEvent.click(screen.getByRole("button", { name: DISMISS }));
    expect(screen.queryByRole("button", { name: DISMISS })).toBeNull();
    first.unmount();

    render(<ConsentBanner />);
    expect(screen.queryByRole("button", { name: DISMISS })).toBeNull();
  });
});

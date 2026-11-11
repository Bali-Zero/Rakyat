import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";
import { AppShareBar } from "./AppShareBar";

describe("AppShareBar", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", {
      clipboard: { writeText },
      vibrate: vi.fn(() => true),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows Copied ✓ for 1500 ms and reports a successful copy", async () => {
    vi.useFakeTimers();
    const onShare = vi.fn();
    const { getByRole } = render(
      <AppShareBar
        url="https://example.test/post"
        title="A post"
        onShare={onShare}
      />,
    );

    await act(async () => {
      fireEvent.click(getByRole("button", { name: "Copy link" }));
    });

    expect(getByRole("button", { name: "Copied ✓" })).toBeTruthy();
    expect(onShare).toHaveBeenCalledWith("copy");

    act(() => {
      vi.advanceTimersByTime(1500);
    });

    expect(getByRole("button", { name: "Copy link" })).toBeTruthy();
  });

  it("clears a pending copied timer when unmounted", async () => {
    vi.useFakeTimers();
    const { getByRole, unmount } = render(
      <AppShareBar url="https://example.test/post" title="A post" />,
    );

    await act(async () => {
      fireEvent.click(getByRole("button", { name: "Copy link" }));
    });
    expect(vi.getTimerCount()).toBe(1);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });

  it("schedules no timer when unmounted while the copy is pending", async () => {
    vi.useFakeTimers();
    let resolveWrite: () => void = () => {};
    writeText.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        resolveWrite = resolve;
      }),
    );
    const { getByRole, unmount } = render(
      <AppShareBar url="https://example.test/post" title="A post" />,
    );

    fireEvent.click(getByRole("button", { name: "Copy link" }));
    unmount();
    await act(async () => {
      resolveWrite();
    });

    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps Copied ✓ for 1500 ms after the last copy", async () => {
    vi.useFakeTimers();
    const { getByRole } = render(
      <AppShareBar url="https://example.test/post" title="A post" />,
    );

    await act(async () => {
      fireEvent.click(getByRole("button", { name: "Copy link" }));
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    await act(async () => {
      fireEvent.click(getByRole("button", { name: "Copied ✓" }));
    });

    expect(vi.getTimerCount()).toBe(1);

    act(() => {
      vi.advanceTimersByTime(1499);
    });
    expect(getByRole("button", { name: "Copied ✓" })).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(getByRole("button", { name: "Copy link" })).toBeTruthy();
  });

  it("does not show Copied ✓ or schedule a timer when copying is rejected", async () => {
    vi.useFakeTimers();
    writeText.mockRejectedValueOnce(new Error("clipboard denied"));
    const { getByRole, queryByRole } = render(
      <AppShareBar url="https://example.test/post" title="A post" />,
    );

    await act(async () => {
      fireEvent.click(getByRole("button", { name: "Copy link" }));
    });

    expect(queryByRole("button", { name: "Copied ✓" })).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });
});

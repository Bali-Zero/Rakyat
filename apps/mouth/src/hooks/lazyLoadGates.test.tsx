import React, { useRef } from "react";
import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAfterPageIdle } from "./useAfterPageIdle";
import { useInViewOnce } from "./useInViewOnce";

describe("useAfterPageIdle", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("stays false until the browser reports idle, then true", () => {
    let fire: (() => void) | null = null;
    vi.stubGlobal(
      "requestIdleCallback",
      vi.fn((cb: () => void) => {
        fire = cb;
        return 1;
      }),
    );
    vi.stubGlobal("cancelIdleCallback", vi.fn());
    const { result } = renderHook(() => useAfterPageIdle());
    expect(result.current).toBe(false);
    act(() => fire!());
    expect(result.current).toBe(true);
  });

  it("falls back to a short timeout where requestIdleCallback is missing", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestIdleCallback", undefined);
    const { result } = renderHook(() => useAfterPageIdle());
    expect(result.current).toBe(false);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(true);
  });
});

function Probe() {
  const ref = useRef<HTMLDivElement>(null);
  const seen = useInViewOnce(ref);
  return <div ref={ref}>{seen ? "seen" : "not yet"}</div>;
}

describe("useInViewOnce", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("flips once the element intersects, and disconnects", () => {
    let callback: IntersectionObserverCallback | null = null;
    const disconnect = vi.fn();
    vi.stubGlobal(
      "IntersectionObserver",
      vi.fn(function (this: unknown, cb: IntersectionObserverCallback) {
        callback = cb;
        return { observe: vi.fn(), disconnect, unobserve: vi.fn() };
      }),
    );
    render(<Probe />);
    expect(screen.getByText("not yet")).toBeInTheDocument();
    act(() =>
      callback!(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      ),
    );
    expect(screen.getByText("seen")).toBeInTheDocument();
    expect(disconnect).toHaveBeenCalled();
  });

  it("is true at once without IntersectionObserver", () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    render(<Probe />);
    expect(screen.getByText("seen")).toBeInTheDocument();
  });
});

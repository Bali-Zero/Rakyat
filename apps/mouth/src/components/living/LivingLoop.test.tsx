import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { LivingLoop, claimPeak, releasePeak, __resetPeakForTests } from "./LivingLoop";
import type { LivingAsset } from "@/lib/living/manifest";

const asset: LivingAsset = {
  id: "z1",
  kind: "loop",
  poster: "/video/z1.webp",
  width: 1280,
  height: 720,
  sources: { vp9: "/video/z1.webm", h264: "/video/z1.mp4" },
};

let ioCallback: ((entries: Partial<IntersectionObserverEntry>[]) => void) | null = null;
let reducedMotion = false;

beforeEach(() => {
  __resetPeakForTests();
  ioCallback = null;
  reducedMotion = false;
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(cb: (e: Partial<IntersectionObserverEntry>[]) => void) {
        ioCallback = cb;
      }
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: q.includes("reduce") ? reducedMotion : false,
    addEventListener() {},
    removeEventListener() {},
  }));
  vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockImplementation((t: string) =>
    t.includes("vp9") ? "probably" : "",
  );
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function (this: HTMLMediaElement) {
    Object.defineProperty(this, "paused", { value: false, configurable: true });
    this.dispatchEvent(new Event("play"));
    return Promise.resolve();
  });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function (this: HTMLMediaElement) {
    Object.defineProperty(this, "paused", { value: true, configurable: true });
    this.dispatchEvent(new Event("pause"));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const enterViewport = async () => {
  await act(async () => {
    ioCallback?.([{ isIntersecting: true, intersectionRatio: 1 }]);
  });
};

describe("LivingLoop", () => {
  it("renders the poster with no video source before entering the viewport", () => {
    const { container } = render(<LivingLoop asset={asset} />);
    const video = container.querySelector("video")!;
    expect(video.getAttribute("poster")).toBe("/video/z1.webp");
    expect(video.getAttribute("src")).toBeNull();
    expect(video.getAttribute("preload")).toBe("none");
    expect(video.getAttribute("aria-hidden")).toBe("true");
  });

  it("assigns the VP9 source and plays once in view", async () => {
    const { container } = render(<LivingLoop asset={asset} />);
    await enterViewport();
    const video = container.querySelector("video")!;
    expect(video.getAttribute("src")).toBe("/video/z1.webm");
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });

  it("never loads a video under prefers-reduced-motion", async () => {
    reducedMotion = true;
    const { container } = render(<LivingLoop asset={asset} />);
    await enterViewport();
    expect(container.querySelector("video")!.getAttribute("src")).toBeNull();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("does not load while externally paused", async () => {
    const { container } = render(<LivingLoop asset={asset} paused />);
    await enterViewport();
    expect(container.querySelector("video")!.getAttribute("src")).toBeNull();
  });

  it("does not load while it is not the active card", async () => {
    const { container } = render(<LivingLoop asset={asset} active={false} />);
    await enterViewport();
    expect(container.querySelector("video")!.getAttribute("src")).toBeNull();
  });

  it("renders the fallback when the asset is not in the manifest", () => {
    const { getByText } = render(<LivingLoop asset={null} fallback={<span>static</span>} />);
    expect(getByText("static")).toBeTruthy();
  });

  it("reserves its box from the asset dimensions (no layout shift)", () => {
    const { container } = render(<LivingLoop asset={asset} />);
    const box = container.querySelector("[data-living-loop]") as HTMLElement;
    expect(box.style.aspectRatio).toBe("1280 / 720");
  });
});

describe("the R3 conductor", () => {
  it("pauses the previous peak when another living video starts", () => {
    const a = document.createElement("video");
    const b = document.createElement("video");
    void a.play();
    claimPeak(a);
    void b.play();
    claimPeak(b);
    expect(a.paused).toBe(true);
    expect(b.paused).toBe(false);
  });

  it("forgets a peak that paused on its own", () => {
    const a = document.createElement("video");
    const b = document.createElement("video");
    void a.play();
    claimPeak(a);
    a.pause();
    releasePeak(a);
    void b.play();
    claimPeak(b);
    expect(b.paused).toBe(false);
  });
});

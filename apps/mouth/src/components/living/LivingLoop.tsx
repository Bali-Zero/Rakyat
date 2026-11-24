"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useInView } from "@/hooks/useInView";
import {
  AV1_CONTENT_TYPE,
  motionAllowed,
  pickSource,
  type CodecProbe,
  type LivingAsset,
} from "@/lib/living/manifest";

/**
 * One silent cinemagraph loop (Linea 7, spec §1.2–§1.3).
 *
 * - The poster (frame 0 of the same encode) is always rendered: it is the
 *   LCP candidate and the whole experience under reduced motion, Save-Data
 *   or a poor network.
 * - The video source is assigned only once the element is in view; before
 *   that the page downloads zero video bytes (`preload="none"`, no src).
 * - It pauses when it leaves the viewport, when the tab is hidden, when the
 *   caller pauses it (e.g. Visa Oracle's own Pause button) or when it is not
 *   the `active` card of a grid.
 * - R3 "one peak per viewport" is enforced by the module-level conductor
 *   below: any living video that starts playing pauses the one that was.
 */

let currentPeak: HTMLVideoElement | null = null;

/** Called on every living video's `play`: the previous peak, if any, pauses. */
export function claimPeak(video: HTMLVideoElement): void {
  if (currentPeak && currentPeak !== video && !currentPeak.paused) currentPeak.pause();
  currentPeak = video;
}

export function releasePeak(video: HTMLVideoElement): void {
  if (currentPeak === video) currentPeak = null;
}

/** Test seam only. */
export function __resetPeakForTests(): void {
  currentPeak = null;
}

function browserCodecProbe(): CodecProbe {
  const probe = document.createElement("video");
  return {
    canPlayType: (type) => probe.canPlayType(type),
    av1PowerEfficient: async () => {
      const mc = (navigator as Navigator & { mediaCapabilities?: MediaCapabilities }).mediaCapabilities;
      if (!mc?.decodingInfo) return null;
      try {
        const info = await mc.decodingInfo({
          type: "file",
          video: {
            contentType: AV1_CONTENT_TYPE,
            width: 1280,
            height: 720,
            bitrate: 600_000,
            framerate: 24,
          },
        });
        return info.supported && info.powerEfficient;
      } catch {
        return null;
      }
    },
  };
}

function useMotionAllowed(): boolean {
  const [allowed, setAllowed] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    // Network Information API: Chromium only, absent from the DOM lib types.
    const conn = (navigator as Navigator & {
      connection?: Partial<EventTarget> & { saveData?: boolean; effectiveType?: string };
    }).connection;
    const update = () =>
      setAllowed(
        motionAllowed({
          reducedMotion: mq?.matches ?? false,
          saveData: conn?.saveData,
          effectiveType: conn?.effectiveType,
        }),
      );
    update();
    mq?.addEventListener?.("change", update);
    conn?.addEventListener?.("change", update);
    return () => {
      mq?.removeEventListener?.("change", update);
      conn?.removeEventListener?.("change", update);
    };
  }, []);
  return allowed;
}

function usePageVisible(): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

export interface LivingLoopProps {
  asset: LivingAsset | null;
  /** In a grid, only the active card may move (hover/focus, or the centred card on mobile). */
  active?: boolean;
  /** External pause, e.g. Visa Oracle's `data-motion="paused"`. */
  paused?: boolean;
  className?: string;
  /** Rendered when the asset is not (yet) in the manifest. */
  fallback?: ReactNode;
  /** Crop the box to another ratio (e.g. "21 / 9" for a banner); the video covers it. Defaults to the asset's own. */
  aspectRatio?: string;
}

export function LivingLoop({
  asset,
  active = true,
  paused = false,
  className,
  fallback = null,
  aspectRatio,
}: LivingLoopProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const inView = useInView(boxRef);
  const allowed = useMotionAllowed();
  const pageVisible = usePageVisible();
  const [src, setSrc] = useState<string | null>(null);

  const shouldPlay = Boolean(asset) && allowed && inView && active && !paused && pageVisible;

  // Pick the source the first time the loop is allowed to play; keep it after.
  useEffect(() => {
    if (!asset || src || !shouldPlay) return;
    let cancelled = false;
    void pickSource(asset.sources, browserCodecProbe()).then((chosen) => {
      if (!cancelled) setSrc(chosen);
    });
    return () => {
      cancelled = true;
    };
  }, [asset, src, shouldPlay]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !src) return;
    if (shouldPlay) {
      video.muted = true; // React does not reflect `muted` as a property; autoplay needs it.
      void video.play().catch(() => {
        /* autoplay refused: the poster stays, which is the designed fallback */
      });
    } else if (!video.paused) {
      video.pause();
    }
  }, [shouldPlay, src]);

  useEffect(() => {
    const video = videoRef.current;
    return () => {
      if (video) releasePeak(video);
    };
  }, []);

  if (!asset) return <>{fallback}</>;

  return (
    <div
      ref={boxRef}
      className={className}
      data-living-loop={asset.id}
      style={{ aspectRatio: aspectRatio ?? `${asset.width} / ${asset.height}`, position: "relative" }}
    >
      <video
        ref={videoRef}
        aria-hidden="true"
        tabIndex={-1}
        muted
        loop
        playsInline
        disablePictureInPicture
        disableRemotePlayback
        preload="none"
        poster={asset.poster}
        src={src ?? undefined}
        width={asset.width}
        height={asset.height}
        onPlay={(e) => claimPeak(e.currentTarget)}
        onPause={(e) => releasePeak(e.currentTarget)}
        style={{ display: "block", width: "100%", height: "100%", objectFit: "cover" }}
      />
    </div>
  );
}

/**
 * Warms the HTTP cache with the source this browser would pick, so a loop that
 * appears a moment later (Visa Oracle Z2, shown while the verdict is computed)
 * starts from cache instead of competing with the evaluation request.
 *
 * It renders a `<link rel="prefetch">` (allowed in <body>): the browser fetches
 * at idle priority, outside the page's own `fetch` traffic, and keeps the file
 * when this component unmounts. Nothing is rendered when motion is not allowed
 * or the asset is unknown.
 */
export function LivingPrefetch({ asset, enabled = true }: { asset: LivingAsset | null; enabled?: boolean }) {
  const allowed = useMotionAllowed();
  const [href, setHref] = useState<string | null>(null);
  useEffect(() => {
    if (!asset || !allowed || !enabled) return;
    let cancelled = false;
    void pickSource(asset.sources, browserCodecProbe()).then((src) => {
      if (!cancelled) setHref(src);
    });
    return () => {
      cancelled = true;
    };
  }, [asset, allowed, enabled]);
  if (!href || !allowed || !enabled) return null;
  return <link rel="prefetch" href={href} />;
}

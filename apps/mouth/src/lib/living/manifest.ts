/**
 * Living Web (Linea 7) — the manifest of every cinemagraph loop and verdict
 * capsule the site may play, plus the pure rules that decide WHETHER and
 * WHICH file plays. No React here: everything is unit-testable.
 *
 * Spec: research/linea-7-living-web/2026-11-21-sito-vivente-spec.md §1.2, §2.1.4.
 *
 * An asset is listed only once its files exist under public/video/living/
 * and passed the file QA (loop seam, poster = frame 0, size budget, no audio
 * track). Filenames carry the first 8 hex of their sha256, so the URLs can be
 * cached as immutable. An unlisted id renders nothing but its caller's own
 * fallback.
 */

export type LivingKind = "loop" | "capsule";

export interface LivingSources {
  /** AV1 in MP4 — served only where decoding is power-efficient. */
  av1?: string;
  /** VP9 in WebM. */
  vp9?: string;
  /** H.264 in MP4 — the last resort every browser can play. */
  h264: string;
}

export interface LivingAsset {
  id: string;
  kind: LivingKind;
  /** WebP extracted from the exact first frame of the same encode. */
  poster: string;
  /** Optional 4:3 centre crop of the same frame, for grid cards. */
  cardPoster?: string;
  width: number;
  height: number;
  sources: LivingSources;
  /** WebVTT captions — required for capsules (they speak). */
  captions?: string;
  /** Capsules only: the RulePack sequence and product version they were scripted against. */
  packSequence?: number;
  productVersionId?: string;
  productCode?: string;
}

/** Byte budgets per encode (spec §1.2). Checked by the file QA, mirrored here for tests. */
export const LIVING_BUDGET_BYTES: Record<LivingKind, number> = {
  loop: 450 * 1024,
  capsule: 1200 * 1024,
};

/**
 * Pilot batch (2026-11-22): five KBLI sector worlds and Zantara's "reflection"
 * loop (Z2). Keyframes approved by Zero; seams closed with a tail→head
 * crossfade and encoded with a single keyframe so the loop point does not pop.
 */
export const LIVING_ASSETS: Readonly<Record<string, LivingAsset>> = Object.freeze({
  "kbli-i": {
    id: "kbli-i",
    kind: "loop",
    poster: "/video/living/kbli-i.6a2e7f29.webp",
    cardPoster: "/video/living/kbli-i-card.16b22631.webp",
    width: 1280,
    height: 720,
    sources: {
      vp9: "/video/living/kbli-i.ad7cfcc5.webm",
      h264: "/video/living/kbli-i.f61b24d7.mp4",
    },
  },
  "kbli-l": {
    id: "kbli-l",
    kind: "loop",
    poster: "/video/living/kbli-l.dc03ebb5.webp",
    cardPoster: "/video/living/kbli-l-card.86cac9a3.webp",
    width: 1280,
    height: 720,
    sources: {
      vp9: "/video/living/kbli-l.f949b983.webm",
      h264: "/video/living/kbli-l.b988643e.mp4",
    },
  },
  "kbli-f": {
    id: "kbli-f",
    kind: "loop",
    poster: "/video/living/kbli-f.c4327204.webp",
    cardPoster: "/video/living/kbli-f-card.1a8166cc.webp",
    width: 1280,
    height: 720,
    sources: {
      vp9: "/video/living/kbli-f.9e584f53.webm",
      h264: "/video/living/kbli-f.f3c16982.mp4",
    },
  },
  "kbli-m": {
    id: "kbli-m",
    kind: "loop",
    poster: "/video/living/kbli-m.94e91afc.webp",
    cardPoster: "/video/living/kbli-m-card.11a5fc8a.webp",
    width: 1280,
    height: 720,
    sources: {
      vp9: "/video/living/kbli-m.b360aacf.webm",
      h264: "/video/living/kbli-m.6d029280.mp4",
    },
  },
  "kbli-g": {
    id: "kbli-g",
    kind: "loop",
    poster: "/video/living/kbli-g.f91139e0.webp",
    cardPoster: "/video/living/kbli-g-card.6a4048c1.webp",
    width: 1280,
    height: 720,
    sources: {
      vp9: "/video/living/kbli-g.2a801a77.webm",
      h264: "/video/living/kbli-g.849d7c62.mp4",
    },
  },
  "oracle-z2": {
    id: "oracle-z2",
    kind: "loop",
    poster: "/video/living/oracle-z2.d81c141d.webp",
    cardPoster: "/video/living/oracle-z2-card.1bc0af2d.webp",
    width: 1280,
    height: 720,
    sources: {
      vp9: "/video/living/oracle-z2.d0ac1857.webm",
      h264: "/video/living/oracle-z2.d0492ce2.mp4",
    },
  },
});

export function livingAsset(
  id: string,
  registry: Readonly<Record<string, LivingAsset>> = LIVING_ASSETS,
): LivingAsset | null {
  return registry[id] ?? null;
}

/** The loop of a KBLI section (A…U), or null while that world is not filmed yet. */
export function kbliSectionLoop(
  sectionId: string,
  registry: Readonly<Record<string, LivingAsset>> = LIVING_ASSETS,
): LivingAsset | null {
  return livingAsset(`kbli-${sectionId.toLowerCase()}`, registry);
}

/**
 * The sequence lock (spec §2.1.4): a capsule is offered only when it was
 * scripted against the SAME pack sequence and the SAME product version the
 * verdict on screen came from. A new pack hides every capsule until it is
 * re-rendered — a capsule can never speak a superseded rule.
 */
export function capsuleFor(
  productCode: string,
  verdict: { packSequence: number | null | undefined; productVersionId: string | null | undefined },
  registry: Readonly<Record<string, LivingAsset>> = LIVING_ASSETS,
): LivingAsset | null {
  if (verdict.packSequence == null || !verdict.productVersionId) return null;
  for (const asset of Object.values(registry)) {
    if (
      asset.kind === "capsule" &&
      asset.productCode === productCode &&
      asset.packSequence === verdict.packSequence &&
      asset.productVersionId === verdict.productVersionId &&
      asset.captions
    ) {
      return asset;
    }
  }
  return null;
}

export interface MotionEnvironment {
  reducedMotion: boolean;
  saveData?: boolean;
  effectiveType?: string;
}

const POOR_NETWORKS = new Set(["slow-2g", "2g", "3g"]);

/** Whether any video may load at all. Otherwise the poster is the whole experience. */
export function motionAllowed(env: MotionEnvironment): boolean {
  if (env.reducedMotion) return false;
  if (env.saveData) return false;
  if (env.effectiveType && POOR_NETWORKS.has(env.effectiveType)) return false;
  return true;
}

export const AV1_CONTENT_TYPE = 'video/mp4; codecs="av01.0.05M.08"';
export const VP9_CONTENT_TYPE = 'video/webm; codecs="vp9"';
export const H264_CONTENT_TYPE = 'video/mp4; codecs="avc1.640028"';

export interface CodecProbe {
  /** `HTMLMediaElement.canPlayType` of a throwaway video element. */
  canPlayType: (type: string) => string;
  /** `navigator.mediaCapabilities.decodingInfo` resolved to powerEfficient, or null when unavailable. */
  av1PowerEfficient: () => Promise<boolean | null>;
}

/**
 * AV1 only when the device decodes it in hardware (powerEfficient). Then VP9
 * where the browser can play it, then H.264. Software AV1 on a mid-range
 * phone is exactly the battery drain this contract exists to prevent.
 */
export async function pickSource(sources: LivingSources, probe: CodecProbe): Promise<string> {
  if (sources.av1 && probe.canPlayType(AV1_CONTENT_TYPE) !== "") {
    if ((await probe.av1PowerEfficient()) === true) return sources.av1;
  }
  if (sources.vp9 && probe.canPlayType(VP9_CONTENT_TYPE) !== "") return sources.vp9;
  return sources.h264;
}

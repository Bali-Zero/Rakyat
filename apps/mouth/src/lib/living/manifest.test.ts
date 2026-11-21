import { describe, it, expect } from "vitest";
import { existsSync, statSync } from "node:fs";
import path from "node:path";
import {
  AV1_CONTENT_TYPE,
  LIVING_ASSETS,
  LIVING_BUDGET_BYTES,
  kbliSectionLoop,
  VP9_CONTENT_TYPE,
  capsuleFor,
  motionAllowed,
  pickSource,
  type CodecProbe,
  type LivingAsset,
} from "./manifest";

const capsule = (over: Partial<LivingAsset> = {}): LivingAsset => ({
  id: "capsule-e28a",
  kind: "capsule",
  poster: "/video/capsule-e28a.webp",
  width: 720,
  height: 720,
  sources: { h264: "/video/capsule-e28a.mp4" },
  captions: "/video/capsule-e28a.en.vtt",
  packSequence: 33,
  productVersionId: "c8277316-c29d-5fde-bf9f-2611099a0029",
  productCode: "E28A",
  ...over,
});

const probe = (playable: string[], av1Efficient: boolean | null): CodecProbe => ({
  canPlayType: (t) => (playable.includes(t) ? "probably" : ""),
  av1PowerEfficient: async () => av1Efficient,
});

const publicFile = (url: string) => path.join(__dirname, "../../../public", url);

describe("LIVING_ASSETS", () => {
  const assets = Object.values(LIVING_ASSETS);

  it("lists the pilot batch", () => {
    expect(Object.keys(LIVING_ASSETS).sort()).toEqual(
      ["kbli-f", "kbli-g", "kbli-i", "kbli-l", "kbli-m", "oracle-z2"],
    );
  });

  it.each(assets.map((a) => [a.id, a] as const))("%s: every listed file exists", (_id, asset) => {
    const urls = [asset.poster, asset.cardPoster, asset.sources.av1, asset.sources.vp9, asset.sources.h264];
    for (const url of urls.filter(Boolean) as string[]) {
      expect(existsSync(publicFile(url)), url).toBe(true);
    }
  });

  it.each(assets.map((a) => [a.id, a] as const))("%s: every encode fits the byte budget", (_id, asset) => {
    for (const url of Object.values(asset.sources).filter(Boolean) as string[]) {
      expect(statSync(publicFile(url)).size, url).toBeLessThanOrEqual(LIVING_BUDGET_BYTES[asset.kind]);
    }
  });

  it.each(assets.map((a) => [a.id, a] as const))("%s: filenames are content-hashed", (_id, asset) => {
    for (const url of [asset.poster, ...Object.values(asset.sources)].filter(Boolean) as string[]) {
      expect(url).toMatch(/\.[0-9a-f]{8}\.(webm|mp4|webp)$/);
    }
  });
});

describe("kbliSectionLoop", () => {
  it("finds a filmed section regardless of case", () => {
    expect(kbliSectionLoop("I")?.id).toBe("kbli-i");
    expect(kbliSectionLoop("g")?.id).toBe("kbli-g");
  });
  it("returns null for a section not filmed yet", () => {
    expect(kbliSectionLoop("A")).toBeNull();
  });
});

describe("capsuleFor — the sequence lock", () => {
  const registry = { e28a: capsule() };
  const verdict = { packSequence: 33, productVersionId: "c8277316-c29d-5fde-bf9f-2611099a0029" };

  it("offers the capsule when code, sequence and product version all match", () => {
    expect(capsuleFor("E28A", verdict, registry)?.id).toBe("capsule-e28a");
  });

  it("hides it when the pack sequence moved on", () => {
    expect(capsuleFor("E28A", { ...verdict, packSequence: 34 }, registry)).toBeNull();
  });

  it("hides it when the product version changed inside the same sequence", () => {
    expect(capsuleFor("E28A", { ...verdict, productVersionId: "other" }, registry)).toBeNull();
  });

  it("hides it when the verdict carries no sequence", () => {
    expect(capsuleFor("E28A", { packSequence: null, productVersionId: verdict.productVersionId }, registry)).toBeNull();
  });

  it("never offers a capsule without captions", () => {
    expect(capsuleFor("E28A", verdict, { e28a: capsule({ captions: undefined }) })).toBeNull();
  });

  it("never offers a loop as a capsule", () => {
    expect(capsuleFor("E28A", verdict, { e28a: capsule({ kind: "loop" }) })).toBeNull();
  });
});

describe("motionAllowed", () => {
  it("allows motion by default", () => {
    expect(motionAllowed({ reducedMotion: false })).toBe(true);
  });
  it.each([
    [{ reducedMotion: true }],
    [{ reducedMotion: false, saveData: true }],
    [{ reducedMotion: false, effectiveType: "3g" }],
    [{ reducedMotion: false, effectiveType: "slow-2g" }],
  ])("refuses motion for %o", (env) => {
    expect(motionAllowed(env)).toBe(false);
  });
  it("keeps motion on 4G", () => {
    expect(motionAllowed({ reducedMotion: false, effectiveType: "4g" })).toBe(true);
  });
});

describe("pickSource", () => {
  const sources = { av1: "/v.av1.mp4", vp9: "/v.webm", h264: "/v.mp4" };

  it("serves AV1 only when the device decodes it power-efficiently", async () => {
    expect(await pickSource(sources, probe([AV1_CONTENT_TYPE, VP9_CONTENT_TYPE], true))).toBe("/v.av1.mp4");
  });
  it("skips software AV1 and falls to VP9", async () => {
    expect(await pickSource(sources, probe([AV1_CONTENT_TYPE, VP9_CONTENT_TYPE], false))).toBe("/v.webm");
  });
  it("treats an unknown power profile as software", async () => {
    expect(await pickSource(sources, probe([AV1_CONTENT_TYPE, VP9_CONTENT_TYPE], null))).toBe("/v.webm");
  });
  it("falls to H.264 when neither AV1 nor VP9 is playable", async () => {
    expect(await pickSource(sources, probe([], true))).toBe("/v.mp4");
  });
  it("works with an H.264-only asset", async () => {
    expect(await pickSource({ h264: "/only.mp4" }, probe([VP9_CONTENT_TYPE], true))).toBe("/only.mp4");
  });
});

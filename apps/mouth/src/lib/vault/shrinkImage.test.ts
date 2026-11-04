import { afterEach, describe, expect, it, vi } from "vitest";
import { shrinkImage } from "./shrinkImage";

function file(type: string, size = 16): File {
  return new File([new Uint8Array(size)], "x", { type });
}

describe("shrinkImage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("only re-encodes JPEG/PNG", async () => {
    expect(await shrinkImage(file("application/pdf"), 10)).toBeNull();
  });

  it("returns null (never throws) when the browser cannot decode images", async () => {
    vi.stubGlobal("createImageBitmap", undefined);
    expect(await shrinkImage(file("image/jpeg"), 10)).toBeNull();
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn().mockRejectedValue(new Error("decode failed")),
    );
    expect(await shrinkImage(file("image/png"), 10)).toBeNull();
  });
});

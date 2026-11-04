/**
 * Re-encode a JPEG/PNG photo in the browser so it fits under `maxBytes`.
 *
 * Phone photos of a passport are often 4–8 MB, above what the upload proxy
 * can carry (see `uploadLimits.ts`). Downscaling to a few thousand pixels
 * keeps a document photo readable while bringing it to 1–2 MB.
 *
 * Returns a new JPEG `File` at or under `maxBytes`, or `null` when the
 * browser cannot decode/encode the image or no attempt fits. Never throws.
 */
const ATTEMPTS: ReadonlyArray<{ maxSide: number; quality: number }> = [
  { maxSide: 2560, quality: 0.85 },
  { maxSide: 2048, quality: 0.8 },
  { maxSide: 1600, quality: 0.75 },
];

function jpegName(name: string): string {
  const base = name.replace(/\.[^./\\]+$/, "");
  return `${base || "photo"}.jpg`;
}

function toBlob(
  canvas: HTMLCanvasElement,
  quality: number,
): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), "image/jpeg", quality);
  });
}

export async function shrinkImage(
  file: File,
  maxBytes: number,
): Promise<File | null> {
  if (file.type !== "image/jpeg" && file.type !== "image/png") return null;
  if (typeof createImageBitmap !== "function") return null;
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    return null;
  }
  try {
    for (const { maxSide, quality } of ATTEMPTS) {
      const scale = Math.min(
        1,
        maxSide / Math.max(bitmap.width, bitmap.height),
      );
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const ctx = canvas.getContext("2d");
      if (!ctx) return null;
      ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const blob = await toBlob(canvas, quality);
      if (blob && blob.size <= maxBytes) {
        return new File([blob], jpegName(file.name), { type: "image/jpeg" });
      }
    }
    return null;
  } catch {
    return null;
  } finally {
    bitmap.close?.();
  }
}

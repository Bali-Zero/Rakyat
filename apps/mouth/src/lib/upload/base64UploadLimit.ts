/**
 * Size cap for uploads sent as base64 inside a JSON body.
 *
 * These requests go through the Next catch-all proxy
 * (`app/api/[...path]/route.ts`), a Vercel Function that refuses a request
 * body above ~4.5 MB with `413 FUNCTION_PAYLOAD_TOO_LARGE` (measured 19 Oct
 * 2026: 6 MB refused by the platform, 1 MB reached the backend). Base64 adds
 * one third, so a 10 MB promise could never be kept: 3 MB of file becomes
 * 4 MB of base64, which leaves room for the JSON around it.
 */
export const PROXY_BODY_LIMIT_BYTES = 4.5 * 1024 * 1024;
export const BASE64_UPLOAD_MAX_BYTES = 3 * 1024 * 1024;
export const BASE64_UPLOAD_MAX_LABEL = "3 MB";

/** Bytes a file of `size` occupies once base64-encoded. */
export function base64Size(size: number): number {
  return Math.ceil(size / 3) * 4;
}

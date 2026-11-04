/**
 * Upload size cap for portal uploads.
 *
 * The backend accepts 10 MB, but every portal upload goes through the Next
 * catch-all proxy (`app/api/[...path]/route.ts`), which runs as a Vercel
 * Function. Vercel refuses a request body above ~4.5 MB with
 * `413 FUNCTION_PAYLOAD_TOO_LARGE` before our code runs (measured 19 Oct
 * 2026: 1 MB -> 401 from the backend, 6 MB -> 413 from the platform). A
 * browser often reports that refusal as a bare "Network error". The cap is
 * therefore the platform's, minus room for the multipart envelope.
 * NEXT_PUBLIC_VAULT_MAX_SIZE may LOWER it, never raise it above the platform.
 */
export const PLATFORM_MAX_BYTES = 4 * 1024 * 1024;
const raw = process.env.NEXT_PUBLIC_VAULT_MAX_SIZE;
const parsed = raw ? Number(raw) : PLATFORM_MAX_BYTES;
export const MAX_SIZE_BYTES =
  Number.isFinite(parsed) && parsed > 0
    ? Math.min(parsed, PLATFORM_MAX_BYTES)
    : PLATFORM_MAX_BYTES;
export const MAX_SIZE_MB_LABEL = `${Math.floor(MAX_SIZE_BYTES / 1024 / 1024)} MB`;

/**
 * Upload MIME allowlist — enforced client-side only as a defensive UX check.
 * The backend has its own validation; this prevents obvious mistakes before
 * bytes go over the wire.
 */
export const ALLOWED_UPLOAD_MIMES = [
  "application/pdf",
  "image/jpeg",
  "image/png",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
] as const satisfies readonly string[];

export const UPLOAD_ACCEPT = ".pdf,.jpg,.jpeg,.png,.docx";
export const UPLOAD_FORMAT_LABEL = `PDF, JPG, PNG, or DOCX up to ${MAX_SIZE_MB_LABEL}`;

export type AllowedUploadMime = (typeof ALLOWED_UPLOAD_MIMES)[number];

export function isAllowedUploadMime(mime: string): boolean {
  return (ALLOWED_UPLOAD_MIMES as readonly string[]).includes(mime);
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { beginPortalAction } from "@/lib/portal-analytics";
import { sanitizeFilename } from "@/lib/vault/sanitizeFilename";
import { shrinkImage } from "@/lib/vault/shrinkImage";
import {
  MAX_SIZE_BYTES,
  MAX_SIZE_MB_LABEL,
  isAllowedUploadMime,
} from "@/lib/vault/uploadLimits";
import {
  VaultUploadResponse,
  type VaultUploadedFile,
} from "@/lib/schemas/vault";

export type UploadState =
  | { status: "idle" }
  | { status: "validating" }
  | { status: "uploading"; progress: number }
  | {
      status: "done";
      file: VaultUploadedFile;
    }
  | { status: "error"; message: string; httpStatus?: number };

export interface UploadOptions {
  practiceId?: number | string | null;
  documentType?: string;
  /**
   * FASE 5 — client-facing note on why this document is being provided.
   * Sent as the `document_purpose` form field; trimmed/capped server-side.
   */
  purpose?: string | null;
}

interface RetryableUpload {
  readonly file: File;
  readonly options: UploadOptions;
  /** The file is already the re-encoded picture: a retry must not redo it. */
  readonly pictureAlone: boolean;
}

function isRetryableHttpStatus(status: number): boolean {
  return (
    status === 0 ||
    status === 408 ||
    status === 425 ||
    status === 429 ||
    status >= 500
  );
}

/**
 * Uploads a single file to `POST /api/portal/documents/upload` via
 * `XMLHttpRequest` (required for progress events — `fetch` has no upload
 * progress in browsers).
 *
 * Pre-flight validation rejects disallowed MIME types and oversize files
 * before opening the request. The upload response is Zod-validated against
 * the backend's client-safe document projection; scan, OCR, and storage
 * internals are intentionally unavailable to the UI.
 */
/**
 * The reason the server gave, not just the number it gave it with.
 *
 * The failure branch used to render `Upload failed (${status})` and throw
 * `xhr.responseText` away — even though the success branch two lines above
 * already parses it. A client hitting the hour-long duplicate guard saw
 * "Upload failed (409)" instead of "A file with this name was uploaded less
 * than an hour ago", and a client hitting a document-type rule saw
 * "Upload failed (422)" instead of the rule (portal audit ux F1).
 *
 * FastAPI puts the reason in `detail`; this app's own envelopes use
 * `message`. Anything else — HTML from a proxy, an empty body, a non-string
 * `detail` (FastAPI's validation errors are a LIST of objects, which is
 * exactly the shape that must not be stringified at a client) — falls back
 * to the status code, which is still more honest than a wrong sentence.
 */
function serverRejectionMessage(responseText: string, status: number): string {
  if (status === 413) return tooLargeMessage();
  const fallback = `Upload failed (${status})`;
  try {
    const body: unknown = JSON.parse(responseText);
    if (typeof body !== "object" || body === null) return fallback;
    const { detail, message } = body as {
      detail?: unknown;
      message?: unknown;
    };
    for (const candidate of [detail, message]) {
      if (typeof candidate === "string" && candidate.trim().length > 0) {
        return candidate.trim();
      }
    }
    return fallback;
  } catch {
    return fallback;
  }
}

/**
 * The server takes one picture per JPEG, with nothing after it, and answers
 * 415 for anything else. A camera can write more than that into a `.jpg`: a
 * second picture after the first (a gain map, a depth map) or a trailer of
 * its own. The browser reads such a file as the photo it is, so a refused
 * JPEG is sent once more as the picture alone, re-encoded the way a photo
 * above the size cap already is. One more attempt, never a loop.
 */
function isRefusedPhoto(status: number, file: File): boolean {
  return status === 415 && file.type === "image/jpeg";
}

function tooLargeMessage(): string {
  return `This file is too large to upload (max ${MAX_SIZE_MB_LABEL}). Try a smaller photo, a screenshot, or a scanned PDF.`;
}

function readCsrfCookie(): string | null {
  if (typeof document === "undefined") return null;
  const row = document.cookie
    .split("; ")
    .find((entry) => entry.startsWith("nz_csrf_token="));
  return row ? row.slice("nz_csrf_token=".length) : null;
}

export function useVaultUpload() {
  const [state, setState] = useState<UploadState>({ status: "idle" });
  const retryableUploadRef = useRef<RetryableUpload | null>(null);
  // One run per upload, retry or reset the caller makes, and one when the
  // page goes away. Whatever belongs to an earlier run is late: see `current`.
  const runRef = useRef(0);

  useEffect(
    () => () => {
      runRef.current += 1;
    },
    [],
  );

  const reset = useCallback(() => {
    runRef.current += 1;
    retryableUploadRef.current = null;
    setState({ status: "idle" });
  }, []);

  const send = useCallback(function sendFile(
    file: File,
    opts: UploadOptions,
    pictureAlone: boolean,
  ) {
    // Every answer of this request is for this run only. Once the caller has
    // reset, started another upload, retried or left the page, a late answer
    // still closes its usage attempt, and changes neither what is shown nor
    // what a retry would send.
    const run = runRef.current;
    const current = () => runRef.current === run;
    const show = (next: UploadState) => {
      if (current()) setState(next);
    };
    const forgetRetry = () => {
      if (current()) retryableUploadRef.current = null;
    };
    const attempt = beginPortalAction("document_upload", () => ({
      role: api.getUserProfile?.()?.role,
      impersonating: api.getPortalImpersonation?.() !== null,
    }));
    retryableUploadRef.current = null;
    setState({ status: "validating" });

    if (!isAllowedUploadMime(file.type)) {
      attempt.fail("validation");
      setState({
        status: "error",
        message: `File type not allowed: ${file.type || "unknown"}`,
      });
      return;
    }
    if (file.size > MAX_SIZE_BYTES) {
      attempt.fail("validation");
      setState({
        status: "error",
        message: `File exceeds ${MAX_SIZE_MB_LABEL} limit. ${tooLargeMessage()}`,
      });
      return;
    }

    retryableUploadRef.current = {
      file,
      options: { ...opts },
      pictureAlone,
    };
    const fd = new FormData();
    fd.append("file", file, sanitizeFilename(file.name));
    if (opts.practiceId != null && opts.practiceId !== "") {
      fd.append("practice_id", String(opts.practiceId));
    }
    fd.append("document_type", opts.documentType?.trim() || "other");
    if (opts.purpose != null && opts.purpose.trim() !== "") {
      fd.append("document_purpose", opts.purpose.trim());
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/portal/documents/upload");
    xhr.withCredentials = true;
    // The backend checks the double-submit CSRF pair on a cookie-authenticated
    // write, and the proxy no longer copies the cookie into the header.
    const csrfToken = readCsrfCookie();
    if (csrfToken) {
      xhr.setRequestHeader("X-CSRF-Token", csrfToken);
    }

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        show({
          status: "uploading",
          progress: (e.loaded / e.total) * 100,
        });
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const json = JSON.parse(xhr.responseText);
          const parsed = VaultUploadResponse.parse(json);
          if (!parsed.success) {
            attempt.fail("response");
            forgetRetry();
            show({
              status: "error",
              message: parsed.message ?? "Upload rejected",
              httpStatus: xhr.status,
            });
            return;
          }
          forgetRetry();
          attempt.complete();
          show({
            status: "done",
            file: parsed.data,
          });
        } catch {
          attempt.fail("response");
          forgetRetry();
          show({
            status: "error",
            message: "Invalid server response",
            httpStatus: xhr.status,
          });
        }
      } else {
        attempt.fail(xhr.status >= 500 ? "server" : "request");
        if (!isRetryableHttpStatus(xhr.status)) {
          forgetRetry();
        }
        const refusal: UploadState = {
          status: "error",
          message: serverRejectionMessage(xhr.responseText, xhr.status),
          httpStatus: xhr.status,
        };
        if (!pictureAlone && current() && isRefusedPhoto(xhr.status, file)) {
          show({ status: "validating" });
          void shrinkImage(file, MAX_SIZE_BYTES)
            .catch(() => null)
            .then((picture) => {
              if (!current()) return;
              if (picture) sendFile(picture, opts, true);
              else show(refusal);
            });
          return;
        }
        show(refusal);
      }
    };

    xhr.onerror = () => {
      attempt.fail("network");
      show({ status: "error", message: "Network error" });
    };

    xhr.send(fd);
  }, []);

  const upload = useCallback(
    (file: File, opts: UploadOptions = {}) => {
      runRef.current += 1;
      send(file, opts, false);
    },
    [send],
  );

  const retry = useCallback(() => {
    const retryableUpload = retryableUploadRef.current;
    if (!retryableUpload) return;
    runRef.current += 1;
    send(
      retryableUpload.file,
      retryableUpload.options,
      retryableUpload.pictureAlone,
    );
  }, [send]);

  return {
    state,
    upload,
    reset,
    retry,
    canRetry: state.status === "error" && retryableUploadRef.current !== null,
  };
}

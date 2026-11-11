"use client";

import { useEffect, useState } from "react";
import { safeStorage } from "@/lib/utils/storage";

/**
 * Who is signed in, as this app itself records it: the `user_profile` item
 * that `ApiClient` writes on login and removes on logout (lib/api/client.ts,
 * `setUserProfile` / `clearToken`), read through the same `safeStorage`.
 *
 * `ApiClient.getUserProfile()` is deliberately NOT used: it keeps its
 * in-memory copy when the stored item disappears, which is exactly the
 * cross-tab logout case this hook has to see.
 *
 * Signals: the `storage` event (another tab logged in, out or switched
 * account; same precedent as components/terminal/TerminalStatusBar.tsx),
 * plus `visibilitychange`/`focus` as a re-check for changes made in this tab
 * that raise no event. Returns an opaque identity key, or null when nobody is
 * signed in. The key is never logged or rendered.
 */
export function readSessionIdentity(): string | null {
  const raw = safeStorage.getItem("user_profile");
  if (!raw) return null;
  try {
    const p = JSON.parse(raw) as { id?: unknown; email?: unknown } | null;
    const id = p?.id != null ? String(p.id) : "";
    const email = typeof p?.email === "string" ? p.email.toLowerCase() : "";
    return id || email ? `${id}|${email}` : null;
  } catch {
    return null;
  }
}

export function useSessionIdentity(): string | null {
  const [identity, setIdentity] = useState<string | null>(() =>
    readSessionIdentity(),
  );
  useEffect(() => {
    const recheck = () => setIdentity(readSessionIdentity());
    const onVisible = () => {
      if (document.visibilityState === "visible") recheck();
    };
    recheck();
    window.addEventListener("storage", recheck);
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("storage", recheck);
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return identity;
}

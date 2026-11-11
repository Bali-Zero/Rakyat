"use client";

import { useEffect, useState } from "react";
import { safeStorage } from "@/lib/utils/storage";
import { SESSION_CHANGED_EVENT } from "@/lib/auth/session-signal";

/**
 * Who is signed in, as this app itself records it: the `user_profile` item
 * that `ApiClient` writes on login and removes on logout (lib/api/client.ts,
 * `setUserProfile` / `clearToken`), read through the same `safeStorage`.
 *
 * `ApiClient.getUserProfile()` is deliberately NOT used: it keeps its
 * in-memory copy when the stored item disappears, which is exactly the
 * cross-tab logout case this hook has to see.
 *
 * Signals: the `storage` event (another tab; same precedent as
 * components/terminal/TerminalStatusBar.tsx), `bz:session-changed` (this
 * tab's own sign-in/out, dispatched by the writer of `user_profile`), and
 * `visibilitychange`/`focus` as a backstop. Returns the signed-in team member
 * id (STAFF_CARD.md C11 compares it with the card's `team_member_id`), or null
 * when nobody is signed in or the id is unreadable. Never logged or rendered.
 */
export function readSessionIdentity(): string | null {
  const raw = safeStorage.getItem("user_profile");
  if (!raw) return null;
  try {
    const p = JSON.parse(raw) as { id?: unknown } | null;
    const id =
      typeof p?.id === "string" || typeof p?.id === "number"
        ? String(p.id)
        : "";
    return id.trim() ? id : null;
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
    window.addEventListener(SESSION_CHANGED_EVENT, recheck);
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("storage", recheck);
      window.removeEventListener(SESSION_CHANGED_EVENT, recheck);
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return identity;
}

"use client";

import { useEffect, useState } from "react";

import { safeStorage } from "@/lib/utils/storage";
import { decodeJwtPayload, isTokenExpired } from "@/lib/utils/token";
import {
  AUTH_TOKEN_STORAGE_KEY,
  SESSION_CHANGED_EVENT,
  getSessionEpoch,
  isSessionStorageEvent,
} from "./session-signal";

/**
 * Who is signed in, for pages that show personal data. Spec: ./SESSION.md.
 *
 * The identity is the `sub` of the CURRENTLY VALID bearer token in this tab's
 * storage, and nothing else. The cookie and `user_profile` are not sources.
 * No client-side signature check (the backend verifies); the token and the
 * `sub` are never logged or rendered by this module.
 */
export interface SessionIdentity {
  sub: string;
  epoch: number;
}

const CLOCK_SKEW_SECONDS = 30;
// setTimeout cannot take a delay above 2^31-1 ms.
const MAX_TIMER_MS = 2_147_483_647;

function readValidToken(): string | null {
  try {
    const token = safeStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
    if (!token || isTokenExpired(token, CLOCK_SKEW_SECONDS)) return null;
    return token;
  } catch {
    return null;
  }
}

/**
 * Pure read: never removes a token and never dispatches (safe in render).
 * Null for an absent, expired, malformed or `sub`-less token. Never throws.
 */
export function readSessionIdentity(): SessionIdentity | null {
  const token = readValidToken();
  if (!token) return null;
  const payload = decodeJwtPayload(token);
  const sub = payload?.sub;
  if (typeof sub !== "string" || sub.length === 0) return null;
  return { sub, epoch: getSessionEpoch() };
}

export function isSameIdentity(
  a: SessionIdentity | null,
  b: SessionIdentity | null,
): boolean {
  return !!a && !!b && a.sub === b.sub && a.epoch === b.epoch;
}

/**
 * The binding rule for a response (S4): accept it only if the identity at
 * response time (a FRESH `readSessionIdentity()`, not a React state value)
 * equals the one at issue time, and, when the response names its owner (e.g.
 * `team_member_id`), the owner is that identity's `sub`.
 */
export function acceptForIdentity(
  issued: SessionIdentity | null,
  current: SessionIdentity | null,
  owner?: string | number | null,
): boolean {
  if (!issued || !current || !isSameIdentity(issued, current)) return false;
  if (owner === undefined || owner === null) return true;
  return String(owner) === current.sub;
}

/**
 * Call `cb` when the session may have changed: same-tab signal, `storage`
 * event on the session keys (other tabs), and a re-check on tab focus /
 * visibility. Returns the unsubscribe function.
 */
export function subscribeSessionChange(cb: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onSignal = () => cb();
  const onStorage = (event: StorageEvent) => {
    if (isSessionStorageEvent(event)) cb();
  };
  const onVisibility = () => {
    if (document.visibilityState === "visible") cb();
  };
  window.addEventListener(SESSION_CHANGED_EVENT, onSignal);
  window.addEventListener("storage", onStorage);
  window.addEventListener("focus", onSignal);
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    window.removeEventListener(SESSION_CHANGED_EVENT, onSignal);
    window.removeEventListener("storage", onStorage);
    window.removeEventListener("focus", onSignal);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}

/** Milliseconds until the stored token counts as expired, or null if unknown. */
function msUntilExpiry(): number | null {
  const token = readValidToken();
  const exp = token ? decodeJwtPayload(token)?.exp : undefined;
  if (typeof exp !== "number") return null;
  return Math.max(0, (exp - CLOCK_SKEW_SECONDS) * 1000 - Date.now()) + 50;
}

/**
 * React view of the identity. Null on the server and on the first client
 * render (no hydration mismatch), then the live identity. Re-reads on every
 * session signal and when the token reaches its expiry (nobody has to remove
 * it for the page to notice).
 */
export function useSessionIdentity(): SessionIdentity | null {
  const [identity, setIdentity] = useState<SessionIdentity | null>(null);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      const next = readSessionIdentity();
      setIdentity((prev) =>
        prev === null && next === null
          ? prev
          : isSameIdentity(prev, next)
            ? prev
            : next,
      );
      if (timer !== undefined) clearTimeout(timer);
      const ms = msUntilExpiry();
      timer =
        ms === null
          ? undefined
          : setTimeout(refresh, Math.min(ms, MAX_TIMER_MS));
    };
    refresh();
    const unsubscribe = subscribeSessionChange(refresh);
    return () => {
      unsubscribe();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, []);

  return identity;
}

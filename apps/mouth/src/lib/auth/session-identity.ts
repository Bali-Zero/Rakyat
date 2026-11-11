"use client";

import { useEffect, useState } from "react";

import { decodeJwtPayload } from "@/lib/utils/token";
import {
  TOKEN_CLOCK_SKEW_SECONDS,
  readStoredValidSub,
  readStoredValidToken,
} from "./session-credential";
import {
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

// setTimeout cannot take a delay above 2^31-1 ms.
const MAX_TIMER_MS = 2_147_483_647;

/**
 * Pure read: never removes a token and never dispatches (safe in render).
 * Null for an absent, expired, malformed or `sub`-less token. Never throws.
 */
export function readSessionIdentity(): SessionIdentity | null {
  const sub = readStoredValidSub();
  return sub ? { sub, epoch: getSessionEpoch() } : null;
}

export function isSameIdentity(
  a: SessionIdentity | null,
  b: SessionIdentity | null,
): boolean {
  return !!a && !!b && a.sub === b.sub && a.epoch === b.epoch;
}

/**
 * The binding rule for a response carrying personal data (SESSION.md S4).
 * `response` is what `requestWithCredential()` returned. Accepted only if ALL
 * hold:
 *  (a) the request was sent WITH a bearer (`sentSub` non-null): a cookie-only
 *      response is never accepted, the server may have used someone else's
 *      cookie;
 *  (b) a FRESH synchronous read of storage NOW gives a valid token whose sub
 *      equals `sentSub` (no reliance on events or on the epoch);
 *  (c) when the response names its owner (e.g. `team_member_id`), the owner is
 *      `sentSub`;
 *  (d) the identity the page is displaying (`displayed`, from
 *      `useSessionIdentity`) is `sentSub`, and its epoch is still this tab's
 *      epoch (an extra same-tab guard, not the proof).
 */
export function acceptResponse(
  response: { sentSub: string | null },
  displayed: SessionIdentity | null,
  owner?: string | number | null,
): boolean {
  const sentSub = response.sentSub;
  if (!sentSub) return false;
  const nowSub = readStoredValidSub();
  if (nowSub !== sentSub) return false;
  if (owner !== undefined && owner !== null && String(owner) !== sentSub) {
    return false;
  }
  if (!displayed || displayed.sub !== sentSub) return false;
  return displayed.epoch === getSessionEpoch();
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
  const token = readStoredValidToken();
  const exp = token ? decodeJwtPayload(token)?.exp : undefined;
  if (typeof exp !== "number") return null;
  return Math.max(0, (exp - TOKEN_CLOCK_SKEW_SECONDS) * 1000 - Date.now()) + 50;
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

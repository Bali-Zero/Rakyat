import type { QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { clientDetailQueryOptions } from "@/hooks/useClientDetail";
import { earlyKey, startEarly } from "./components/early-fetch";

/**
 * Early start for the client detail page's first-screen data.
 *
 * The workspace layout renders `children` only after the viewer's profile and
 * the INTAKE gate probe have answered, so a fetch started by the page itself
 * waits for hydration + auth + gate (measured 2026-11-18: the profile request
 * began ~5 s after navigation). The layout calls this as soon as auth is
 * confirmed, in PARALLEL with the gate probe; the page then finds the profile
 * in the React Query cache and the portal status in early-fetch.
 *
 * Nothing here widens access: the same authenticated client calls the same
 * endpoints the page would, the backend still decides what this viewer may
 * read, and nothing renders until the layout's gate has cleared.
 */

const CLIENT_DETAIL_PATH = /^\/clients\/(\d+)\/?$/;

/** The numeric client id of a `/clients/{id}` path, else null. */
export function clientIdFromPath(pathname: string | null | undefined) {
  const match = pathname ? CLIENT_DETAIL_PATH.exec(pathname) : null;
  if (!match) return null;
  const id = Number(match[1]);
  return id > 0 ? id : null;
}

/** Starts the profile and the portal status for `clientId`. Idempotent. */
export function prefetchClientDetail(
  queryClient: QueryClient,
  clientId: number,
): void {
  if (!(clientId > 0)) return;
  // prefetchQuery dedupes against an in-flight or fresh entry, so the page's
  // own useQuery mount reuses this request instead of firing a second one.
  void queryClient.prefetchQuery(clientDetailQueryOptions(clientId));
  startEarly(earlyKey("portal-status", clientId), () =>
    api.crm.getPortalStatus(clientId),
  );
}

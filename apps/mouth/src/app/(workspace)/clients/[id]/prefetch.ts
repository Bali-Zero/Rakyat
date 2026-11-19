import type { QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  clientDetailQueryOptions,
  clientTimelineQueryOptions,
  documentCategoriesQueryOptions,
} from "@/hooks/useClientDetail";
import { lkpmApi } from "@/lib/api/workspace/lkpm.api";
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

/**
 * The id-only calls whose owners take them through early-fetch (PortalAccess,
 * PortalMessages, AiSummaryCard, WaCaseIntelligencePanel, the LKPM count).
 * All start at once — none waits for another or for the profile. Called by
 * the layout (first load) and by the page on mount (client-side navigation,
 * where the layout does not re-run); startEarly makes the second call a no-op.
 */
export function startClientDetailEarly(clientId: number): void {
  if (!(clientId > 0)) return;
  startEarly(earlyKey("portal-status", clientId), () =>
    api.crm.getPortalStatus(clientId),
  );
  startEarly(earlyKey("portal-messages", clientId), () =>
    api.crm.getPortalMessages(clientId),
  );
  startEarly(earlyKey("ai-summary", clientId), () =>
    api.crm.getClientAiSummary(clientId),
  );
  startEarly(earlyKey("wa-case-intel", clientId), () =>
    api.crm.getClientWaCaseIntelligence(clientId),
  );
  startEarly(earlyKey("lkpm-history", clientId), () =>
    lkpmApi.getClientHistory(clientId),
  );
}

/**
 * Everything the first screen of `/clients/{id}` reads, started in parallel:
 * the React Query entries the page's hooks read (profile, timeline, document
 * categories) plus the early-fetch calls above. Idempotent — prefetchQuery
 * dedupes against an in-flight or fresh entry, so the page's own useQuery
 * mounts reuse these requests instead of firing a second set.
 */
export function prefetchClientDetail(
  queryClient: QueryClient,
  clientId: number,
): void {
  if (!(clientId > 0)) return;
  void queryClient.prefetchQuery(clientDetailQueryOptions(clientId));
  void queryClient.prefetchQuery(clientTimelineQueryOptions(clientId));
  void queryClient.prefetchQuery(documentCategoriesQueryOptions());
  startClientDetailEarly(clientId);
}

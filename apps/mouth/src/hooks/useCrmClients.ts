/**
 * useCrmClients Hook
 *
 * Hook ottimizzato per gestione clienti CRM con caching e sincronizzazione
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Client, CreateClientParams } from "@/lib/api/crm/crm.types";
import { logger } from "@/lib/logger";

interface UseCrmClientsOptions {
  status?: string;
  assigned_to?: string;
  search?: string;
  nationality?: string;
  passport_expiring_days?: number;
  limit?: number;
  enabled?: boolean;
}

interface ClientsResponse {
  clients: Client[];
  total: number;
  hasMore: boolean;
}

/** The pages of a list before the one being asked for, with the filter they belong to. */
interface EarlierPages {
  filterKey: string;
  /** Offset of the page being asked for. */
  offset: number;
  /** The rows of every page before that offset. */
  clients: Client[];
}

const NO_CLIENTS: Client[] = [];

const logError = (...args: unknown[]) => {
  logger.error(
    "[CRM]",
    {},
    args[0] instanceof Error ? args[0] : new Error(String(args[0])),
  );
};

const debug = (...args: unknown[]) => {
  logger.debug("[CRM] " + args.join(" "), {});
};

/**
 * Hook per gestione lista clienti
 */
export function useCrmClients(options: UseCrmClientsOptions = {}) {
  const {
    status,
    assigned_to,
    search,
    nationality,
    passport_expiring_days,
    limit = 50,
    enabled = true,
  } = options;
  const queryClient = useQueryClient();

  // The list is the list of ONE filter. Every filter that is part of the
  // query is part of this key, so a change of any of them starts a new list
  // in the very render that sees it: nothing has to empty the old one first.
  const filterKey = JSON.stringify([
    status ?? null,
    assigned_to ?? null,
    search ?? null,
    nationality ?? null,
    passport_expiring_days ?? null,
  ]);

  // The pages BEFORE the one being asked for, kept with the filter they
  // belong to. The page being asked for is never copied into state: it is
  // read from the query in the render. So data served from the cache is shown
  // at once, no row of another filter is ever shown under this one, and only
  // the page that is in the list says whether there is more.
  const [earlier, setEarlier] = useState<EarlierPages>(() => ({
    filterKey,
    offset: 0,
    clients: NO_CLIENTS,
  }));
  const offset = earlier.filterKey === filterKey ? earlier.offset : 0;
  const earlierClients =
    earlier.filterKey === filterKey ? earlier.clients : NO_CLIENTS;

  // Forget the pages of a filter the list has left, so that coming back to it
  // starts from its first page again. The render that sees a filter change
  // does not wait for this: it has already started the new list above.
  useEffect(() => {
    setEarlier((kept) =>
      kept.filterKey === filterKey
        ? kept
        : { filterKey, offset: 0, clients: NO_CLIENTS },
    );
  }, [filterKey]);

  // Base query key (without offset — we accumulate results)
  const queryKey = [
    "crm",
    "clients",
    { status, assigned_to, search, nationality, passport_expiring_days },
  ];

  const {
    data,
    isLoading,
    isError,
    error: queryError,
    refetch,
  } = useQuery({
    queryKey: [...queryKey, offset],
    queryFn: async (): Promise<Client[]> => {
      return api.crm.getClients({
        search: search || undefined,
        status: status || undefined,
        assigned_to: assigned_to || undefined,
        nationality: nationality || undefined,
        passport_expiring_days: passport_expiring_days,
        limit,
        offset,
      });
    },
    enabled,
    staleTime: 5 * 60 * 1000,
    gcTime: 10 * 60 * 1000,
  });

  // The earlier pages, then the page the query holds, without repeats.
  const clients = useMemo(() => {
    if (!data) return earlierClients;
    if (offset === 0) return data;
    const seen = new Set(earlierClients.map((c) => c.id));
    return [...earlierClients, ...data.filter((c) => !seen.has(c.id))];
  }, [data, offset, earlierClients]);

  // Only a page that is in the list can say the list has ended.
  const hasMore = data ? data.length === limit : true;

  const loadMore = useCallback(() => {
    // The next page is asked for only once this one is in the list, and only
    // if it was a full one. While it loads, or after it failed, asking for
    // more asks for nothing: a page is never skipped.
    if (!data || data.length < limit) return;
    setEarlier({ filterKey, offset: offset + limit, clients });
  }, [data, limit, filterKey, offset, clients]);

  const reset = useCallback(() => {
    setEarlier({ filterKey, offset: 0, clients: NO_CLIENTS });
    refetch().catch((err) => {
      logger.error(
        "[CRM] Failed to refetch clients",
        {},
        err instanceof Error ? err : new Error(String(err)),
      );
    });
  }, [filterKey, refetch]);

  return {
    clients,
    total: clients.length,
    isLoading: isLoading && offset === 0,
    isError,
    error: queryError,
    loadMore,
    hasMore,
    isLoadingMore: isLoading && offset > 0,
    reset,
    refetch,
  };
}

/**
 * Hook per singolo cliente con caching
 */
export function useCrmClient(clientId: number | null) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["crm", "client", clientId],
    queryFn: async (): Promise<Client> => {
      if (!clientId) throw new Error("Client ID required");
      return api.crm.getClient(clientId);
    },
    enabled: !!clientId,
    staleTime: 2 * 60 * 1000, // 2 minutes
  });

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["crm", "client", clientId] });
  }, [queryClient, clientId]);

  return {
    ...query,
    invalidate,
  };
}

/**
 * Hook per creazione cliente
 */
export function useCreateClient() {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: async ({
      data,
      createdBy,
    }: {
      data: CreateClientParams;
      createdBy: string;
    }) => {
      return api.crm.createClient(data, createdBy);
    },
    onSuccess: (newClient) => {
      // Invalidate clients list
      queryClient.invalidateQueries({ queryKey: ["crm", "clients"] });
      // Add to cache under both keys (plural list key + singular detail key)
      queryClient.setQueryData(["crm", "clients", newClient.id], newClient);
      queryClient.setQueryData(["crm", "client", newClient.id], newClient);
      debug("Client created:", newClient.id);
    },
    onError: (err) => {
      logError("Failed to create client:", err);
    },
  });

  return mutation;
}

/**
 * Hook per aggiornamento cliente
 */
export function useUpdateClient(clientId: number) {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: async ({
      updates,
      updatedBy,
    }: {
      updates: Partial<CreateClientParams>;
      updatedBy: string;
    }) => {
      return api.crm.updateClient(clientId, updates, updatedBy);
    },
    onSuccess: (updatedClient) => {
      // Update cache under both keys (plural list key + singular detail key)
      queryClient.setQueryData(["crm", "clients", clientId], updatedClient);
      queryClient.setQueryData(["crm", "client", clientId], updatedClient);
      // Invalidate list
      queryClient.invalidateQueries({ queryKey: ["crm", "clients"] });
      debug("Client updated:", clientId);
    },
    onError: (err) => {
      logError("Failed to update client:", err);
    },
  });

  return mutation;
}

/**
 * Hook per statistiche CRM
 */
export function useCrmStats() {
  return useQuery({
    queryKey: ["crm", "stats"],
    queryFn: async () => {
      const [clientResult, practiceResult, interactionResult] =
        await Promise.allSettled([
          api.crm.request<{
            total: number;
            by_status: Record<string, number>;
            by_team_member: Array<{ assigned_to: string; count: number }>;
            passport_expired: number;
            passport_expiring_soon: number;
            silent_30d: number;
          }>("/api/crm/clients/stats/overview"),
          api.crm.getPracticeStats(),
          api.crm.getInteractionStats(),
        ]);

      // clientStats backs totalClients, the count clients/page.tsx renders as
      // "N total" with no isError guard of its own — it relies on this
      // query's isError instead. A failed fetch here must surface as a query
      // error, not silently coalesce into 0 (a stats-endpoint 503 was
      // rendering as a legitimate empty book). practice/interaction stats
      // keep degrading independently below, but a failed leg becomes null,
      // never 0: activePractices and revenue are null when getPracticeStats
      // fails, so the page renders "—" instead of a measured zero. revenue is
      // one nullable object — its three figures come from the same response
      // and are absent together.
      if (clientResult.status === "rejected") {
        throw clientResult.reason;
      }
      const clientStats = clientResult.value;
      const practiceStats =
        practiceResult.status === "fulfilled" ? practiceResult.value : null;
      const interactionStats =
        interactionResult.status === "fulfilled"
          ? interactionResult.value
          : null;

      return {
        totalClients: clientStats.total,
        activePractices: practiceStats ? practiceStats.active_practices : null,
        revenue: practiceStats?.revenue
          ? {
              total: practiceStats.revenue.total_revenue,
              paid: practiceStats.revenue.paid_revenue,
              outstanding: practiceStats.revenue.outstanding_revenue,
            }
          : null,
        byStatus: clientStats.by_status ?? {},
        interactions: interactionStats ?? null,
        passportExpired: clientStats.passport_expired ?? 0,
        passportExpiringSoon: clientStats.passport_expiring_soon ?? 0,
        silent30d: clientStats.silent_30d ?? 0,
      };
    },
    staleTime: 5 * 60 * 1000, // 5 minutes
  });
}

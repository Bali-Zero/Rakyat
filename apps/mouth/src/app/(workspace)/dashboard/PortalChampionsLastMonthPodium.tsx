"use client";

import { Trophy } from "lucide-react";
import type { PortalChallengeResponse } from "@/lib/api/dashboard/dashboard.api";
import { ChampionArena } from "./ChampionArena";
import { usePortalChallenge } from "./_lib/usePortalChallenge";

/**
 * Last month's Portal Champion podium. It renders only from the authenticated
 * live payload of the closed round. When the request fails, is still loading,
 * or the round is not closed yet, the section says so plainly instead of
 * showing a stand-in.
 */
export function PortalChampionsLastMonthPodium({
  identity,
}: {
  identity: string;
}) {
  const { data, isLoading } = usePortalChallenge(identity);

  const closedRound: PortalChallengeResponse | null =
    data &&
    Array.isArray(data.entries) &&
    data.entries.length >= 3 &&
    data.status === "closed"
      ? data
      : null;

  return (
    <section
      data-testid="portal-champions-last-month-podium"
      aria-label="Last Month's News — Portal Champion Final Podium"
      className="mt-8 flex flex-col gap-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--bz-border)] pb-2">
        <div className="flex items-center gap-2">
          <Trophy
            className="h-4 w-4 text-[var(--bz-copper-text)]"
            aria-hidden="true"
          />
          <span className="text-[11px] font-extrabold uppercase tracking-[0.14em] text-[var(--bz-copper-text)]">
            Last Month&apos;s News
          </span>
          <span className="text-[12px] font-semibold text-[var(--tx-secondary)]">
            · Official October Podium &amp; Final Results
          </span>
        </div>
        <span className="text-[11px] font-bold uppercase tracking-[0.12em] text-[var(--tx-secondary)]">
          October 2026 Archive
        </span>
      </div>

      {closedRound ? (
        /* Original Blue-Ink Podium (`ChampionArena`) ONLY — all secondary tables stripped */
        <ChampionArena
          data={closedRound}
          countdown={
            <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--bz-kita-ink-panel-copper)] px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--bz-kita-ink-panel-copper)]">
              Selesai
            </span>
          }
        />
      ) : isLoading ? (
        <p
          data-testid="portal-champions-last-month-podium-loading"
          role="status"
          className="text-[12px] text-[var(--tx-secondary)]"
        >
          Loading the final podium…
        </p>
      ) : (
        <p
          data-testid="portal-champions-last-month-podium-empty"
          className="text-[12px] text-[var(--tx-secondary)]"
        >
          There is no closed round to show yet. The final podium appears here
          once its results are available.
        </p>
      )}
    </section>
  );
}

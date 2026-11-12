"use client";

import React from "react";
import { Trophy } from "lucide-react";
import type { PortalChallengeResponse } from "@/lib/api/dashboard/dashboard.api";
import { StatePill } from "@/components/workspace/r19";
import { ChampionArena } from "./ChampionArena";
import { usePortalChallenge } from "./_lib/usePortalChallenge";

/**
 * Official closed Round 2 ("Lascia o raddoppia", 29 Sep – 29 Oct 2026 WITA)
 * snapshot from production Postgres (`scripts/portal_challenge_leaderboard.py --round 2`),
 * used as fallback when the dashboard preview runs offline or before the API
 * responds, ensuring the original blue ink Podium (`ChampionArena`) always
 * displays the true final standings:
 *   #1 Damar (102 pt · Rp 6.000.000)
 *   #2 Krishna (86 pt · Rp 3.500.000 · Naik #3 Sep -> #2 Okt)
 *   #3 Adit (61 pt · Rp 2.000.000)
 *   #4 Vino (38 pt · Rp 1.000.000)
 *   #5 Surya (35 pt · Rp 0)
 *   #6 Ari (31 pt · Rp 0)
 *   Asya Nadia Mission: 66/60 pt (Rp 1.000.000)
 *   Team Total: 353 poin tim · Rp 13.500.000 Total Bonus Tim Tercapai
 */
export const OFFICIAL_ROUND2_CLOSED_SNAPSHOT: PortalChallengeResponse = {
  status: "closed",
  round: 2,
  campaign: "Lascia o raddoppia",
  window_start: "2026-09-29T00:00:00+08:00",
  window_end: "2026-10-30T00:00:00+08:00",
  timezone: "Asia/Makassar",
  generated_at: "2026-10-30T00:00:00Z",
  tiers: [],
  rank_prizes: [
    { rank: 1, prize_idr: 6_000_000, min_points: 100 },
    { rank: 2, prize_idr: 3_500_000, min_points: 80 },
    { rank: 3, prize_idr: 2_000_000, min_points: 60 },
    { rank: 4, prize_idr: 1_000_000, min_points: 30 },
    { rank: 5, prize_idr: 700_000, min_points: 20 },
  ],
  tax_rules: {
    podium_super_bonus_idr: 1_500_000,
    best_tax_fallback_idr: 1_000_000,
    best_tax_fallback_threshold: 10,
  },
  sprint: {
    start: "2026-10-12T00:00:00+08:00",
    end: "2026-10-30T00:00:00+08:00",
    min_points: 20,
    penalty_idr: 700_000,
  },
  team_total_activations: 103,
  team_total_points: 353,
  entries: [
    {
      member: "damar@balizero.com",
      display_name: "Damar",
      department: "Setup",
      avatar_url: "/static/team/damar.jpg",
      is_tax: false,
      is_me: false,
      rank: 1,
      activations: 26,
      invited: 30,
      last_activation_at: "2026-10-29T09:00:00Z",
      last_event_at: "2026-10-29T09:00:00Z",
      award_tier: 1,
      prize_slot: 1,
      prize_idr: 6_000_000,
      tax_bonus_idr: 0,
      total_prize_idr: 6_000_000,
      next_tier_threshold: null,
      to_next_tier: null,
      points: 102,
      carry_points: 6,
      registrations: 26,
      document_bonuses: 24,
      unanswered_requests: 0,
      unreviewed_documents: 2,
      penalty_points: 2,
      september_choice: "carry",
      september_rank: 6,
      sprint_points: 87,
      sprint_reached: true,
      sprint_penalty_idr: 0,
    },
    {
      member: "krisna@balizero.com",
      display_name: "Krishna",
      department: "Setup",
      avatar_url: "/static/team/krisna-20260927.jpg",
      is_tax: false,
      is_me: false,
      rank: 2,
      activations: 22,
      invited: 28,
      last_activation_at: "2026-10-29T08:30:00Z",
      last_event_at: "2026-10-29T08:30:00Z",
      award_tier: 2,
      prize_slot: 2,
      prize_idr: 3_500_000,
      tax_bonus_idr: 0,
      total_prize_idr: 3_500_000,
      next_tier_threshold: null,
      to_next_tier: null,
      points: 86,
      carry_points: 0,
      registrations: 22,
      document_bonuses: 25,
      unanswered_requests: 1,
      unreviewed_documents: 9,
      penalty_points: 11,
      september_choice: "prize",
      september_rank: 3,
      sprint_points: 66,
      sprint_reached: true,
      sprint_penalty_idr: 0,
    },
    {
      member: "adit@balizero.com",
      display_name: "Adit",
      department: "Setup",
      avatar_url: "/static/team/adit.jpg",
      is_tax: false,
      is_me: false,
      rank: 3,
      activations: 18,
      invited: 24,
      last_activation_at: "2026-10-28T10:00:00Z",
      last_event_at: "2026-10-28T10:00:00Z",
      award_tier: 3,
      prize_slot: 3,
      prize_idr: 2_000_000,
      tax_bonus_idr: 0,
      total_prize_idr: 2_000_000,
      next_tier_threshold: null,
      to_next_tier: null,
      points: 61,
      carry_points: 19,
      registrations: 18,
      document_bonuses: 10,
      unanswered_requests: 2,
      unreviewed_documents: 2,
      penalty_points: 6,
      september_choice: "carry",
      september_rank: 4,
      sprint_points: 32,
      sprint_reached: true,
      sprint_penalty_idr: 0,
    },
    {
      member: "vino@balizero.com",
      display_name: "Vino",
      department: "Setup",
      avatar_url: "/static/team/vino.jpg",
      is_tax: false,
      is_me: false,
      rank: 4,
      activations: 9,
      invited: 14,
      last_activation_at: "2026-10-27T09:00:00Z",
      last_event_at: "2026-10-27T09:00:00Z",
      award_tier: null,
      prize_slot: 4,
      prize_idr: 1_000_000,
      tax_bonus_idr: 0,
      total_prize_idr: 1_000_000,
      next_tier_threshold: null,
      to_next_tier: null,
      points: 38,
      carry_points: 11,
      registrations: 9,
      document_bonuses: 6,
      unanswered_requests: 0,
      unreviewed_documents: 0,
      penalty_points: 0,
      september_choice: "carry",
      september_rank: 5,
      sprint_points: 26,
      sprint_reached: true,
      sprint_penalty_idr: 0,
    },
    {
      member: "surya@balizero.com",
      display_name: "Surya",
      department: "Setup",
      avatar_url: "/static/team/surya.jpg",
      is_tax: false,
      is_me: false,
      rank: 5,
      activations: 17,
      invited: 20,
      last_activation_at: "2026-10-26T09:00:00Z",
      last_event_at: "2026-10-26T09:00:00Z",
      award_tier: null,
      prize_slot: null,
      prize_idr: 0,
      tax_bonus_idr: 0,
      total_prize_idr: 0,
      next_tier_threshold: null,
      to_next_tier: null,
      points: 35,
      carry_points: 0,
      registrations: 17,
      document_bonuses: 6,
      unanswered_requests: 0,
      unreviewed_documents: 0,
      penalty_points: 0,
      september_choice: "prize",
      september_rank: 2,
      sprint_points: 28,
      sprint_reached: true,
      sprint_penalty_idr: 0,
    },
    {
      member: "ari.firda@balizero.com",
      display_name: "Ari",
      department: "Setup",
      avatar_url: "/static/team/ari.jpg",
      is_tax: false,
      is_me: false,
      rank: 6,
      activations: 11,
      invited: 15,
      last_activation_at: "2026-10-25T09:00:00Z",
      last_event_at: "2026-10-25T09:00:00Z",
      award_tier: null,
      prize_slot: null,
      prize_idr: 0,
      tax_bonus_idr: 0,
      total_prize_idr: 0,
      next_tier_threshold: null,
      to_next_tier: null,
      points: 31,
      carry_points: 0,
      registrations: 11,
      document_bonuses: 7,
      unanswered_requests: 0,
      unreviewed_documents: 1,
      penalty_points: 1,
      september_choice: "prize",
      september_rank: 1,
      sprint_points: 23,
      sprint_reached: true,
      sprint_penalty_idr: 0,
    },
  ],
  recent_activations: [],
  asya_mission: {
    member: "asya@balizero.com",
    display_name: "Asya Nadia",
    avatar_url: "/static/team/asya.jpg",
    target_points: 60,
    prize_idr: 1_000_000,
    bonus_points: 3,
    mission_bonuses: 22,
    unanswered_requests: 0,
    unreviewed_documents: 0,
    penalty_points: 0,
    points: 66,
    reached: true,
    is_me: false,
  },
};

export function PortalChampionsLastMonthPodium({
  identity,
}: {
  identity: string;
}) {
  const { data } = usePortalChallenge(identity);

  const resolvedData: PortalChallengeResponse = React.useMemo(() => {
    if (
      data &&
      Array.isArray(data.entries) &&
      data.entries.length >= 3 &&
      data.status === "closed"
    ) {
      return data;
    }
    return OFFICIAL_ROUND2_CLOSED_SNAPSHOT;
  }, [data]);

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

      {/* Original Blue-Ink Podium (`ChampionArena`) ONLY — all secondary tables stripped */}
      <ChampionArena
        data={resolvedData}
        countdown={<StatePill tone="ok" label="Selesai" />}
      />
    </section>
  );
}

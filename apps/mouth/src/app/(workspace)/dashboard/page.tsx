"use client";

import React from "react";
import Link from "next/link";
import { DeliveryBonusDesk } from "@/components/hr/DeliveryBonusDesk";
import { RefreshCw } from "lucide-react";
import { DashboardErrorBoundary } from "@/components/ErrorBoundary";
import { dashboardQueryKey, useDashboardData } from "@/hooks/useDashboardData";
import { useRealtime } from "@/lib/realtime";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { logger } from "@/lib/logger";
import { api } from "@/lib/api";
import { STRINGS } from "@/lib/strings";
import { Masthead } from "@/components/workspace/r19";
import { DeskLink, LedgerSection } from "./desk";
import { TeamReelMonthlyHub } from "./TeamReelMonthlyHub";
import { PortalChampionsLastMonthPodium } from "./PortalChampionsLastMonthPodium";
import {
  DashboardNewsCarousel,
  type DashboardNewsArticle,
} from "./DashboardNewsCarousel";
import { VisaOracleTestingBanner } from "./VisaOracleTestingBanner";

// ── Intel feed (latest 6 published articles) ──────────────
function useIntelFeed(identity: string) {
  return useQuery<DashboardNewsArticle[]>({
    queryKey: ["intel-feed", identity],
    queryFn: async () => {
      const data = await api.blog.listArticles(6, 0);
      return ((data.articles ?? []) as DashboardNewsArticle[]).slice(0, 6);
    },
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
    enabled: Boolean(identity),
  });
}

// ── Main page ──────────────────────────────────────────────
export default function DashboardPage() {
  const authIdentity = api.getUserProfile()?.email?.trim().toLowerCase() ?? "";
  const { data: intelArticles, isLoading: intelLoading } =
    useIntelFeed(authIdentity);
  const { user, isLoading, isError, refetch } = useDashboardData(authIdentity);

  const realtime = useRealtime();
  const queryClient = useQueryClient();

  React.useEffect(() => {
    const unsubscribe = realtime.subscribe("dashboard_update", () => {
      if (authIdentity) {
        queryClient.invalidateQueries({
          queryKey: dashboardQueryKey(authIdentity),
        });
      }
    });
    return unsubscribe;
  }, [authIdentity, realtime, queryClient]);

  React.useEffect(() => {
    if (user?.email && !isLoading) {
      realtime.connect(user.email, user.email);
      logger.info("Dashboard loaded", {
        component: "DashboardPage",
        action: "mount",
        user: user.email,
      });
    }
  }, [user?.email, isLoading, realtime]);

  // Loading skeleton
  if (isLoading) {
    return (
      <div className="space-y-3 p-4 md:p-6">
        <div className="h-[104px] w-full animate-pulse bg-[var(--bz-card)]" />
        <div className="h-[340px] w-full animate-pulse bg-[var(--bz-card)]" />
        <div className="h-[460px] w-full animate-pulse bg-[var(--bz-card)]" />
      </div>
    );
  }

  // Error state
  if (isError) {
    return (
      <div className="p-4 md:p-6">
        <Masthead
          eyebrow="Desk"
          title={`Dashboard ${STRINGS.common.errorPrefix}`}
          subtitle="We could not load the desk. Nothing was changed."
          right={
            <button
              onClick={() => refetch()}
              className="inline-flex min-h-11 items-center gap-2 border border-[var(--line-control)] bg-[var(--bz-card)] px-3 text-[12px] font-[650] text-[var(--tx-pure)] hover:bg-[var(--bz-card-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--bz-copper)]"
            >
              <RefreshCw className="h-4 w-4" />
              Retry
            </button>
          }
        />
      </div>
    );
  }

  const today = new Date().toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: "Asia/Makassar",
  });

  return (
    <DashboardErrorBoundary>
      <div className="p-4 md:p-6">
        <Masthead
          eyebrow={
            <span className="text-[var(--bz-copper-text)]">Desk · {today}</span>
          }
          title="Today"
          subtitle="Bali Zero Workspace · Team Updates, Latest Published News & Monthly Podium"
          headingClassName="text-[32px] md:text-[40px]"
          className="mb-5"
          right={
            <Link
              href="/process/new"
              className="inline-flex min-h-11 items-center bg-[var(--state-success)] px-3.5 text-[12px] font-[650] text-[var(--bz-on-warm)] hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--bz-copper)]"
            >
              New process
            </Link>
          }
        />

        <DeliveryBonusDesk />

        {/* Visa Oracle testing campaign (T01-T06) — silent unless the viewer
            is an assigned tester with cases still open today. */}
        <VisaOracleTestingBanner identity={authIdentity} />

        {/* 1. Continuous 9:16 Team Reel + Monthly Highlights Feature Boxes */}
        <TeamReelMonthlyHub />

        {/* 2. Full-Width Tall Latest 6 Published Articles Carousel */}
        <LedgerSection
          ordinal="01"
          title="Intelligence feed"
          meta="Latest 6 articles"
          right={
            <DeskLink href="https://balizero.com/news" external>
              All news
            </DeskLink>
          }
          className="mt-6 w-full"
        >
          <DashboardNewsCarousel
            articles={intelArticles ?? []}
            isLoading={intelLoading}
          />
        </LedgerSection>

        {/* 3. Last Month News — Portal Champions Podium (Blue Background) */}
        <PortalChampionsLastMonthPodium identity={authIdentity} />
      </div>
    </DashboardErrorBoundary>
  );
}

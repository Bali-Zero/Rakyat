"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  FilterBar,
  FilterSelect,
  ListPageHeader,
  StatChips,
} from "@balizero/core";
import { Home, Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  useSecondHomeCases,
  useSecondHomeSummary,
} from "@/hooks/useSecondHome";
import {
  STAGE_GROUP,
  STAGE_LABELS,
  VISIBLE_STAGES,
  isTerminalStage,
} from "@/lib/api/secondhome/state-machine";
import type {
  CaseSummary,
  E33Stage,
  GuaranteeBasis,
} from "@/lib/api/secondhome/secondhome.types";
import { ScannerBadge } from "./components/ScannerBadge";
import { CaseGroupSection } from "./components/CaseListSection";

interface FilterState {
  stage: string;
  basis: string;
  activeOnly: boolean;
}

export default function SecondHomeConsolePage() {
  const router = useRouter();
  const [showFilters, setShowFilters] = useState(false);
  const [filters, setFilters] = useState<FilterState>({
    stage: "",
    basis: "",
    activeOnly: true,
  });

  // "Active only" excludes terminal stages server-side, so it can never be
  // combined with a terminal stage choice (the list would always be empty).
  const activeOnlyApplies =
    filters.activeOnly && !(filters.stage && isTerminalStage(filters.stage));

  const listParams = useMemo(
    () => ({
      stage: filters.stage ? (filters.stage as E33Stage) : undefined,
      basis: filters.basis ? (filters.basis as GuaranteeBasis) : undefined,
      active_only: activeOnlyApplies || undefined,
    }),
    [filters.stage, filters.basis, activeOnlyApplies],
  );

  const summaryQuery = useSecondHomeSummary();
  const casesQuery = useSecondHomeCases(listParams);

  const activeFiltersCount = [
    filters.stage,
    filters.basis,
    activeOnlyApplies ? "active_only" : "",
  ].filter(Boolean).length;

  const clearFilters = () =>
    setFilters({ stage: "", basis: "", activeOnly: true });

  const casesData = casesQuery.data?.cases;
  const cases: CaseSummary[] = useMemo(() => casesData ?? [], [casesData]);
  const grouped = useMemo(
    () => ({
      pipeline: cases.filter((c) => STAGE_GROUP[c.stage] === "pipeline"),
      permit: cases.filter((c) => STAGE_GROUP[c.stage] === "permit"),
      terminal: cases.filter((c) => STAGE_GROUP[c.stage] === "terminal"),
    }),
    [cases],
  );

  const groupCounts = useMemo(() => {
    const byStage = summaryQuery.data?.by_stage ?? {};
    let pipeline = 0;
    let permit = 0;
    let terminal = 0;
    // Terminal = what the backend excludes from "active" (epo, status_change).
    // itap_eval is grouped "terminal" in STAGE_GROUP but is backend-active and
    // currently disabled, so it is counted in no chip.
    for (const [stage, count] of Object.entries(byStage)) {
      const group = STAGE_GROUP[stage as E33Stage];
      const n = count ?? 0;
      if (group === "pipeline") pipeline += n;
      else if (group === "permit") permit += n;
      else if (isTerminalStage(stage)) terminal += n;
    }
    return { pipeline, permit, terminal };
  }, [summaryQuery.data]);

  return (
    <div className="space-y-6">
      <ListPageHeader
        title="Second Home"
        subtitle="E33 Second Home Visa internal console — case entrance, lifecycle, Day-90 guarantee tracking"
        actions={
          <Button
            className="gap-2 bg-[var(--bz-accent)] text-[var(--accent-foreground)] hover:bg-[var(--bz-accent)]/90"
            onClick={() => router.push("/second-home/new")}
          >
            <Plus className="w-4 h-4" />
            New Case
          </Button>
        }
      />

      {/* Scanner arming state — never implies monitoring is active when it isn't */}
      <div className="flex flex-wrap items-center gap-3">
        {summaryQuery.isLoading ? (
          <span className="text-xs text-[var(--bz-text-2)] flex items-center gap-1.5">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading scanner state…
          </span>
        ) : summaryQuery.isError ? (
          <span className="text-xs text-[var(--state-danger)]">
            Could not load scanner state.
          </span>
        ) : summaryQuery.data ? (
          <ScannerBadge state={summaryQuery.data.scan_switch} />
        ) : null}
      </div>

      {/* Stat chips: per-group counts + guarantee due 30d */}
      {summaryQuery.data && (
        <StatChips
          items={[
            {
              key: "pipeline",
              content: <>{groupCounts.pipeline} in pipeline</>,
              className:
                "bg-[var(--surface-raised)] text-[var(--bz-text-2)] border-[var(--bz-border)]",
            },
            {
              key: "permit",
              content: <>{groupCounts.permit} on permit</>,
              className:
                "bg-[var(--surface-raised)] text-[var(--bz-text-2)] border-[var(--bz-border)]",
            },
            {
              key: "terminal",
              content: <>{groupCounts.terminal} terminal (all time)</>,
              className:
                "bg-[var(--surface-raised)] text-[var(--bz-text-2)] border-[var(--bz-border)]",
            },
            {
              key: "active-total",
              content: <>{summaryQuery.data.active_total} active total</>,
              className:
                "bg-[var(--bz-accent)]/10 text-[var(--bz-accent)] border-[var(--bz-accent)]/30",
            },
            summaryQuery.data.guarantee_due_30d > 0 && {
              key: "guarantee-due-30d",
              content: (
                <>
                  ⏰ {summaryQuery.data.guarantee_due_30d} guarantee due in 30d
                </>
              ),
              className:
                "bg-[color-mix(in_srgb,var(--state-warning)_15%,transparent)] text-[var(--state-warning)] border-[color-mix(in_srgb,var(--state-warning)_35%,transparent)]",
            },
          ]}
        />
      )}

      {/* Filters */}
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <Button
            variant={showFilters ? "default" : "outline"}
            className="gap-2 border-[var(--bz-border)] bg-[var(--bz-card)] text-[var(--bz-text-1)] hover:bg-[var(--bz-card-hover)]"
            onClick={() => setShowFilters(!showFilters)}
          >
            Filters
            {activeFiltersCount > 0 && (
              <span className="ml-1 rounded-full bg-[var(--bz-accent)] px-1.5 py-0.5 text-xs text-[var(--accent-foreground)]">
                {activeFiltersCount}
              </span>
            )}
          </Button>
        </div>

        {showFilters && (
          <FilterBar
            activeCount={activeFiltersCount}
            onClearAll={clearFilters}
            className="bz-product-panel rounded-lg"
            gridClassName="grid grid-cols-1 sm:grid-cols-3 gap-4"
          >
            <FilterSelect
              id="stage-filter"
              label="Stage"
              value={filters.stage}
              onChange={(v) => setFilters((f) => ({ ...f, stage: v }))}
              selectClassName="border border-[var(--bz-border)] bg-[var(--bz-base)] text-[var(--bz-text-1)] focus:ring-2 focus:ring-[var(--bz-accent)]/50"
            >
              <option value="">All stages</option>
              {VISIBLE_STAGES.map((stage) => (
                <option key={stage} value={stage}>
                  {STAGE_LABELS[stage]}
                </option>
              ))}
            </FilterSelect>
            <FilterSelect
              id="basis-filter"
              label="Basis"
              value={filters.basis}
              onChange={(v) => setFilters((f) => ({ ...f, basis: v }))}
              selectClassName="border border-[var(--bz-border)] bg-[var(--bz-base)] text-[var(--bz-text-1)] focus:ring-2 focus:ring-[var(--bz-accent)]/50"
            >
              <option value="">Both routes</option>
              <option value="deposit">Deposit route</option>
              <option value="property">Property route</option>
            </FilterSelect>
            <FilterSelect
              id="active-only-filter"
              label="Status"
              value={activeOnlyApplies ? "active" : "all"}
              onChange={(v) =>
                setFilters((f) => ({ ...f, activeOnly: v === "active" }))
              }
              selectClassName="border border-[var(--bz-border)] bg-[var(--bz-base)] text-[var(--bz-text-1)] focus:ring-2 focus:ring-[var(--bz-accent)]/50"
            >
              <option
                value="active"
                disabled={
                  !activeOnlyApplies &&
                  !!filters.stage &&
                  isTerminalStage(filters.stage)
                }
              >
                Active cases only
              </option>
              <option value="all">All cases (incl. terminal)</option>
            </FilterSelect>
            {!activeOnlyApplies &&
              filters.stage &&
              isTerminalStage(filters.stage) && (
                <p
                  className="text-xs sm:col-span-3"
                  style={{ color: "var(--bz-text-2)" }}
                  data-testid="active-only-not-applied"
                >
                  Active-only is not applied to terminal stages.
                </p>
              )}
          </FilterBar>
        )}
      </div>

      {/* Case list — grouped pipeline / permit / terminal */}
      {casesQuery.isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="rounded-xl border border-[var(--bz-border)] bg-[var(--bz-card)]/40 p-4 space-y-3"
            >
              <div className="h-4 bg-[var(--bz-surface)] rounded w-1/2 animate-pulse" />
              <div className="h-16 bg-[var(--bz-surface)] rounded animate-pulse" />
              <div className="h-16 bg-[var(--bz-surface)] rounded animate-pulse" />
            </div>
          ))}
        </div>
      ) : casesQuery.isError ? (
        <div className="flex flex-col items-center justify-center h-32 border border-dashed border-[var(--bz-border)] rounded-lg bg-[var(--bz-card)]/30">
          <p className="text-sm text-[var(--state-danger)]">
            Failed to load cases. Try refreshing the page.
          </p>
        </div>
      ) : cases.length === 0 ? (
        <div className="flex flex-col items-center justify-center h-40 border border-dashed border-[var(--bz-border)] rounded-lg bg-[var(--bz-card)]/30">
          <Home className="w-8 h-8 text-[var(--bz-text-2)] opacity-20 mb-2" />
          <p className="text-sm text-[var(--bz-text-2)]">
            No Second Home cases match these filters
          </p>
          <Button
            variant="outline"
            className="mt-3"
            onClick={() => router.push("/second-home/new")}
          >
            Create the first case
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <CaseGroupSection group="pipeline" items={grouped.pipeline} />
          <CaseGroupSection group="permit" items={grouped.permit} />
          <CaseGroupSection group="terminal" items={grouped.terminal} />
        </div>
      )}
    </div>
  );
}

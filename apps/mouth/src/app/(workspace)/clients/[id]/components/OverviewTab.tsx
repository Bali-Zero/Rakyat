"use client";

import React from "react";
import { ArrowUpRight, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import type {
  AttentionItem,
  ClientProfile,
  ClientDocument,
  ClientCompanyLink,
  FamilyMember,
  Interaction,
} from "@/lib/api/crm/crm.types";
import { PassportCard } from "./PassportCard";
import { VisaCard } from "./VisaCard";
import { ClientInfoCard } from "./ClientInfoCard";
import { VaultCard } from "./VaultCard";
import { AiSummaryCard } from "./AiSummaryCard";
import { WaCaseIntelligencePanel } from "./WaCaseIntelligencePanel";
import { buildLedger, type AttentionTone, type LedgerRow } from "./attention";
import {
  contactFact,
  humanize,
  isBalanceUnknown,
  outstandingAmount,
  pipelineAmount,
  PRACTICE_STAGES,
  stageIndex,
} from "./overview-brief";
import { familyPassport, familyPermit } from "./familyValidity";
import { dayToInstant, resolveCardDay } from "./witaClock";
import type { TabType } from "./types";
import {
  EmptyState,
  LedgerSection,
  NumberedList,
  StatePill,
} from "@/components/workspace/r19";
import { EYEBROW, FOCUS } from "@/components/workspace/r19/tokens";
import { passportLabel } from "../../client-row-model";

/** Whether the Needs-attention ledger renders, so the panels the page mounts
 *  after the Overview continue the numbering instead of repeating "01". */
export function overviewLedgerCount(
  documents: ClientDocument[],
  attention: AttentionItem[] | undefined,
  activePractices: ClientProfile["practices"],
  attentionError: string | null | undefined,
  day: string,
): number {
  const ledger = buildLedger(attention, documents, dayToInstant(day));
  const shown = ledger.rows.length + ledger.more.length > 0 || !!attentionError;
  // + the always-rendered Family & companies section.
  return (shown ? 1 : 0) + (activePractices.length > 0 ? 1 : 0) + 1;
}

/**
 * Where a practice stands on the real lifecycle: one segment per stage, done
 * stages in ink, the current one wider and named. Off-path statuses render
 * nothing (the status word is already in the detail line).
 */
function StageTrack({ status }: { status?: string }) {
  const at = stageIndex(status);
  if (at < 0) return null;
  const stage = PRACTICE_STAGES[at];
  return (
    <span
      className="mt-2 flex items-center gap-2"
      role="img"
      aria-label={`Stage ${at + 1} of ${PRACTICE_STAGES.length}: ${stage.label}`}
      data-testid="stage-track"
    >
      <span className="flex items-center gap-1" aria-hidden="true">
        {PRACTICE_STAGES.map((s, i) => (
          <span
            key={s.key}
            className={`block h-1 rounded-full ${i === at ? "w-6" : "w-3.5"} ${
              i <= at ? "bg-[var(--tx-pure)]" : "bg-[var(--bz-border)]"
            }`}
          />
        ))}
      </span>
      <span className="text-[11px] font-medium text-[var(--tx-secondary)]">
        {stage.label}
      </span>
    </span>
  );
}

const TAG: Record<AttentionTone, string> = {
  strong: "border-[var(--state-warning)] font-bold text-[var(--state-warning)]",
  warning:
    "border-[var(--line-control)] font-semibold text-[var(--state-warning)]",
  info: "border-[var(--line-control)] font-semibold text-[var(--state-info)]",
  success:
    "border-[var(--line-control)] font-semibold text-[var(--state-success)]",
  calm: "border-[var(--bz-border)] text-[var(--tx-secondary)]",
};

/** One small state tag: the state as a WORD in an outline, never colour alone. */
function StateTag({ row }: { row: LedgerRow }) {
  return (
    <span
      className={`inline-flex h-6 shrink-0 items-center rounded-[2px] border px-2 text-[11px] ${TAG[row.tone]}`}
      data-testid="attention-state"
    >
      {row.word}
    </span>
  );
}

function LedgerLine({
  row,
  onReview,
}: {
  row: LedgerRow;
  onReview: (row: LedgerRow) => void;
}) {
  return (
    <li
      className="flex items-start gap-3 border-b border-[var(--bz-border)] py-2.5"
      data-testid={
        row.headline ? `ledger-${row.area.toLowerCase()}` : undefined
      }
    >
      <span className="w-[68px] shrink-0 pt-[3px] text-[12px] text-[var(--tx-secondary)]">
        {row.area}
      </span>
      <span className="min-w-0 flex-1 text-[14px] font-semibold leading-[1.45] text-[var(--tx-pure)]">
        {row.title}
      </span>
      <StateTag row={row} />
      <button
        type="button"
        onClick={() => onReview(row)}
        aria-label={`Review ${row.title}`}
        data-testid="attention-review"
        className={`inline-flex min-h-8 shrink-0 items-center gap-1 rounded px-2 text-[12px] font-medium text-[var(--tx-secondary)] hover:text-[var(--tx-pure)] ${FOCUS}`}
      >
        Review
        <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
      </button>
    </li>
  );
}

export function OverviewTab({
  client,
  stats,
  documents,
  activePractices,
  completedPractices,
  attention,
  attentionError = null,
  needsViewerAction,
  formatDate,
  formatCurrency,
  onEditClick,
  onDeleteClick,
  canDeleteClient,
  isDeletingClient,
  onRefresh,
  clientId,
  familyMembers = [],
  companyLinks = [],
  interactions = [],
  viewerEmail = null,
  onOpenTab = () => {},
  onOpenRemoved,
  day: dayProp,
}: {
  client: ClientProfile["client"];
  stats: ClientProfile["stats"];
  documents: ClientDocument[];
  activePractices: ClientProfile["practices"];
  completedPractices: ClientProfile["practices"];
  /** The backend's computed ledger (`ClientProfile.attention`). Absent, the
   *  ledger shows only the Visa row, built from the current permit. */
  attention?: AttentionItem[];
  /** `ClientProfile.attention_error`: non-null means the list could not be
   *  built, so an empty ledger must not read as "all clear". */
  attentionError?: string | null;
  /** Same ownership predicate the masthead stamp uses — copper is never
   *  derived a second way. */
  needsViewerAction: boolean;
  formatDate: (d: string) => string;
  formatCurrency: (n: number) => string;
  onEditClick: () => void;
  /** Opens the soft-delete confirmation. Only reachable when
   *  `canDeleteClient` is true — the parent owns both. */
  onDeleteClick: () => void;
  /** FALSE while the viewer is still unknown, so the button never appears on
   *  a guess. */
  canDeleteClient: boolean;
  isDeletingClient: boolean;
  onRefresh: () => Promise<void>;
  clientId: number;
  familyMembers?: FamilyMember[];
  companyLinks?: ClientCompanyLink[];
  /** The page's interaction timeline, newest logged contact feeds the brief. */
  interactions?: Interaction[];
  /** Viewer email; null while unknown, so no practice row claims "You". */
  viewerEmail?: string | null;
  onOpenTab?: (tab: TabType) => void;
  /** Opens the Documents "Removed or rejected" group. */
  onOpenRemoved?: () => void;
  /** The card's day (`YYYY-MM-DD`, the payload's `as_of`). */
  day?: string;
}) {
  // The card's day (the payload's `as_of`) is the only day: the instant below
  // is its 00:00 WITA, so every derivation agrees with the server's ledger.
  const day = resolveCardDay(dayProp);
  const now = dayToInstant(day);
  const ledger = buildLedger(attention, documents, now);
  const attentionTotal = ledger.rows.length + ledger.more.length;

  const outstanding = outstandingAmount([
    ...activePractices,
    ...completedPractices,
  ]);
  const balanceUnknown = [...activePractices, ...completedPractices].some(
    isBalanceUnknown,
  );
  const pipeline = pipelineAmount(activePractices);
  const contact = contactFact(client, interactions, now);
  const {
    strategic_recap: strategicRecap,
    strategic_recap_source: recapSource,
  } = client as { strategic_recap?: string; strategic_recap_source?: string };
  // Every titled section is numbered from the ones actually rendered.
  const attentionShown = attentionTotal > 0 || !!attentionError;
  const processesShown = activePractices.length > 0;
  const processesN = (attentionShown ? 1 : 0) + 1;
  const householdN = (attentionShown ? 1 : 0) + (processesShown ? 1 : 0) + 1;

  const practiceDue = (p: ClientProfile["practices"][number]) =>
    outstandingAmount([p]);

  const ownerPill = (assignedTo?: string) => {
    if (assignedTo && viewerEmail && assignedTo === viewerEmail)
      return <StatePill tone="you" label="You" />;
    if (assignedTo)
      return <StatePill tone="ours" label={assignedTo.split("@")[0]} />;
    return <StatePill tone="wait" label="Unassigned" />;
  };

  // A card on this page: scroll it into view, then move focus to it.
  const reviewRow = (row: LedgerRow) => {
    if ("tab" in row.route) {
      onOpenTab(row.route.tab);
      return;
    }
    const el = document.getElementById(row.route.card);
    if (!el) return;
    el.scrollIntoView({ block: "start", behavior: "smooth" });
    el.focus({ preventScroll: true });
  };

  const hasHousehold = familyMembers.length > 0 || companyLinks.length > 0;

  return (
    <div className="space-y-7">
      {/* 1 — Needs attention: one row per area, the rest behind a details */}
      {(attentionTotal > 0 || attentionError) && (
        <LedgerSection
          n={1}
          tone={needsViewerAction ? "you" : "wait"}
          title="Needs attention"
        >
          {attentionError && (
            <p
              role="status"
              data-testid="attention-unavailable"
              className="py-3 text-[13px] text-[var(--tx-secondary)]"
            >
              Attention list unavailable — check the Immigration, Company and
              Tax tabs
            </p>
          )}
          <ul className="py-1" data-testid="attention-rows">
            {ledger.rows.map((row) => (
              <LedgerLine key={row.key} row={row} onReview={reviewRow} />
            ))}
          </ul>
          {ledger.more.length > 0 && (
            <details className="group py-2" data-testid="attention-more">
              <summary
                className={`flex min-h-8 cursor-pointer list-none items-center gap-1.5 text-[12px] font-medium text-[var(--tx-secondary)] hover:text-[var(--tx-pure)] [&::-webkit-details-marker]:hidden ${FOCUS}`}
              >
                <ChevronRight
                  aria-hidden="true"
                  className="h-3 w-3 transition-transform group-open:rotate-90"
                />
                Show {ledger.more.length} more
              </summary>
              <ul>
                {ledger.more.map((row) => (
                  <LedgerLine key={row.key} row={row} onReview={reviewRow} />
                ))}
              </ul>
            </details>
          )}
        </LedgerSection>
      )}

      {/* 2 — Three distinct cards: who, passport, visa */}
      <div
        className="grid grid-cols-1 items-stretch gap-4 md:grid-cols-2 xl:grid-cols-3"
        data-testid="identity-cards"
      >
        <div className="flex md:col-span-2 xl:col-span-1 [&>*]:w-full">
          <ClientInfoCard
            client={client}
            now={now}
            contact={contact}
            formatDate={formatDate}
            onEditClick={onEditClick}
            onDeleteClick={onDeleteClick}
            canDeleteClient={canDeleteClient}
            isDeletingClient={isDeletingClient}
            clientId={clientId}
            viewerEmail={viewerEmail}
            onLogged={onRefresh}
          />
        </div>
        <div className="flex [&>*]:w-full">
          <PassportCard
            client={client}
            documents={documents}
            formatDate={formatDate}
            onRefresh={onRefresh}
            clientId={clientId}
            onOpenRemoved={onOpenRemoved}
            day={day}
          />
        </div>
        <div className="flex [&>*]:w-full">
          <VisaCard
            client={client}
            documents={documents}
            activePractices={activePractices}
            formatDate={formatDate}
            formatCurrency={formatCurrency}
            onRefresh={onRefresh}
            clientId={clientId}
            onOpenRemoved={onOpenRemoved}
            day={day}
          />
        </div>
      </div>

      {/* 3 — The vault: my.balizero.com */}
      <VaultCard
        clientId={clientId}
        clientName={client.full_name}
        clientEmail={client.email}
      />

      {strategicRecap && (
        <div className="border-l-2 border-[var(--line-control)] pl-4">
          <p className={EYEBROW}>
            Strategic recap
            {recapSource && (
              <span
                className="ml-2 font-mono normal-case tracking-normal opacity-70"
                data-testid="recap-source"
              >
                {recapSource.replace(/_/g, " ")}
              </span>
            )}
          </p>
          <p className="mt-1 whitespace-pre-line text-[15px] leading-[1.6] text-[var(--tx-pure)]">
            {strategicRecap}
          </p>
        </div>
      )}

      {/* 4 — Open processes: what is moving, who holds it, what is owed */}
      {activePractices.length > 0 && (
        <LedgerSection
          n={processesN}
          tone="ours"
          title="Open processes"
          actions={
            <Button
              variant="ghost"
              size="sm"
              className="gap-1 text-[var(--tx-secondary)]"
              onClick={() => onOpenTab("process")}
            >
              All practices
              <ChevronRight className="h-3.5 w-3.5" />
            </Button>
          }
        >
          {(pipeline > 0 || outstanding > 0 || balanceUnknown) && (
            <p
              className="flex flex-wrap gap-x-4 gap-y-1 pt-3 text-[12px]"
              data-testid="processes-money"
            >
              {pipeline > 0 && (
                <span className="text-[var(--tx-secondary)]">
                  {formatCurrency(pipeline)} pipeline
                </span>
              )}
              {outstanding > 0 && (
                <span className="font-semibold text-[var(--state-warning)]">
                  {formatCurrency(outstanding)} outstanding
                </span>
              )}
              {balanceUnknown && (
                <span className="text-[var(--tx-secondary)]">
                  {outstanding > 0
                    ? "plus a balance not recorded"
                    : "balance not recorded"}
                </span>
              )}
            </p>
          )}
          <div className="py-3">
            <NumberedList
              items={activePractices.map((p) => {
                const due = practiceDue(p);
                const detail = [
                  humanize(p.status),
                  p.estimated_completion
                    ? `ETA ${formatDate(p.estimated_completion)}`
                    : null,
                  due > 0
                    ? `${formatCurrency(due)} due`
                    : isBalanceUnknown(p)
                      ? "Balance not recorded"
                      : p.payment_status === "paid"
                        ? "Paid"
                        : null,
                ]
                  .filter(Boolean)
                  .join(" · ");
                return {
                  id: `practice-${p.id}`,
                  tone: "ours" as const,
                  title: `${p.practice_type_name || p.title || humanize(p.practice_type_code)}${
                    p.family_member_name ? ` · for ${p.family_member_name}` : ""
                  }`,
                  detail: (
                    <>
                      {detail}
                      <StageTrack status={p.status} />
                    </>
                  ),
                  right: ownerPill(p.assigned_to),
                };
              })}
            />
          </div>
        </LedgerSection>
      )}

      <LedgerSection n={householdN} tone="wait" title="Family & companies">
        {!hasHousehold ? (
          <EmptyState className="border-t-0">
            No family or company linked.
          </EmptyState>
        ) : (
          <ul>
            {familyMembers.map((m) => {
              const permit = familyPermit(m, documents, day);
              const passport = familyPassport(m, documents, day);
              const visaDays = permit.validity?.daysLeft ?? null;
              const passportDays = passport.validity?.daysLeft ?? null;
              const urgent =
                permit.level === "red" ||
                permit.level === "expired" ||
                passport.level === "red" ||
                passport.level === "expired";
              return (
                <li key={`fm-${m.id}`}>
                  <button
                    type="button"
                    onClick={() => onOpenTab("family")}
                    className={`flex w-full items-start gap-3 border-b border-[var(--bz-border)] py-2.5 text-left hover:bg-[var(--bz-card)] ${FOCUS}`}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[14px] font-semibold text-[var(--tx-pure)]">
                        {m.full_name}
                      </span>
                      <span className="mt-0.5 block text-[12px] text-[var(--tx-secondary)]">
                        {humanize(m.relationship)}
                        {m.nationality ? ` · ${m.nationality}` : ""}
                      </span>
                    </span>
                    <span
                      className={`shrink-0 text-right text-[12px] leading-[1.45] ${
                        urgent
                          ? "text-[var(--state-warning)]"
                          : "text-[var(--tx-secondary)]"
                      }`}
                    >
                      {m.current_visa_type ? (
                        <span className="block">
                          {m.current_visa_type}
                          {visaDays !== null
                            ? ` · ${passportLabel(visaDays)}`
                            : ""}
                        </span>
                      ) : null}
                      {passportDays !== null ? (
                        <span className="block">
                          Passport · {passportLabel(passportDays)}
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              );
            })}
            {companyLinks.map((c) => (
              <li key={`co-${c.link_id ?? c.company_id}`}>
                <button
                  type="button"
                  onClick={() => onOpenTab("company")}
                  className={`flex w-full items-start gap-3 border-b border-[var(--bz-border)] py-2.5 text-left hover:bg-[var(--bz-card)] ${FOCUS}`}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[14px] font-semibold text-[var(--tx-pure)]">
                      {c.company_name}
                    </span>
                    <span className="mt-0.5 block text-[12px] text-[var(--tx-secondary)]">
                      {[
                        humanize(c.role),
                        c.company_type,
                        typeof c.ownership_percentage === "number"
                          ? `${c.ownership_percentage}%`
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </span>
                  {c.is_primary && <StatePill tone="wait" label="Primary" />}
                </button>
              </li>
            ))}
          </ul>
        )}
        {typeof stats.company_documents_count === "number" &&
          (stats.company_count ?? 0) > 0 && (
            <p
              className="pt-2.5 text-[12px] text-[var(--tx-secondary)]"
              title="Active documents of every company linked to this client, primary or not."
              data-testid="company-docs-count"
            >
              {stats.company_documents_count} company{" "}
              {stats.company_documents_count === 1 ? "document" : "documents"}{" "}
              on file
            </p>
          )}
      </LedgerSection>

      {/* AI Summary (CRM-Guardian Phase 1 cross-folder L1) */}
      <AiSummaryCard clientId={clientId} section="overview" hideWhenEmpty />
      {/* WhatsApp Case Intelligence — case cards linked to this CRM profile */}
      <WaCaseIntelligencePanel clientId={clientId} />
    </div>
  );
}

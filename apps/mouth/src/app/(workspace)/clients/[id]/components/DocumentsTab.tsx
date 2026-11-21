"use client";

import React, { useMemo, useState } from "react";
import { Archive, Eye, Pencil, Plus } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  ClientCompanyLink,
  ClientDocument,
} from "@/lib/api/crm/crm.types";
import {
  CellStack,
  DeskStrip,
  EmptyState,
  FOCUS,
  HairlineBody,
  HairlineGrid,
  HairlineHead,
  HairlineRow,
  MICRO_LABEL,
  StatePill,
  TABULAR,
  type PillTone,
} from "@/components/workspace/r19";
import { getDocumentOpenUrl } from "./utils";
import {
  URGENT_WINDOW_DAYS,
  VOID_LABEL,
  dayNumber,
  documentValidity,
  parseCalendarDate,
  expiryWording,
  type DocumentValidity,
} from "./documentValidity";
import { resolveCardDay } from "./witaClock";
import { TaxDocumentsSection } from "./TaxDocumentsSection";

/** The filter group that lists every void document of the profile, once. */
const REMOVED_GROUP = "removed";

/** The filter that also shows the tax folders read live from Drive. */
const TAX_GROUP = "tax";

const CATEGORY_ORDER = [
  "immigration",
  "pma",
  "tax",
  "personal",
  "family",
  "other",
];

const CATEGORY_LABELS: Record<string, string> = {
  immigration: "Immigration",
  pma: "Company",
  tax: "Tax",
  personal: "Personal",
  family: "Family",
  other: "Other",
};

/**
 * The grid's column order, desktop width. Below 640px the two hidden columns
 * (Status, Expires) leave the grid — their content relocates onto Document's
 * own secondary line — and the row narrows to Document + Actions, mirroring
 * the same override `ObligationsTable.tsx` uses: `HairlineGrid`'s native
 * `colsCollapsed`+`id` scoped style is inert against the inline `--cols` it
 * sets on the very element it targets (#6520, not this window's to fix), so
 * the override targets the rows/head directly instead of the variable.
 */
const COLS = "minmax(180px,1.7fr) 104px 100px 92px";

/**
 * Status pill, by the shared precedence: a void document reads its reason
 * (Removed, Archived, Rejected) and nothing else; a live one reads its
 * status, and a positive word ("Valid") needs the date to say so. A due DATE
 * is never a tone on the pill (r19: README law 1) — urgency lives on the
 * Expires cell, see `expiryCountdown` below.
 *
 * A document that EXISTS but carries no recognised status (`pending`, or no
 * `status` at all) renders no pill, and never the word "Missing" for a
 * document that is plainly on file.
 */
function documentStatusPill(
  d: ClientDocument,
  v: DocumentValidity,
): { tone: PillTone; label: string } | null {
  if (v.voidReason) return { tone: "wait", label: VOID_LABEL[v.voidReason] };
  if (d.status === "verified") {
    if (v.state === "expired") return { tone: "wait", label: "Expired" };
    if (v.state === "undated") return { tone: "wait", label: "Verified" };
    return { tone: "ok", label: "Valid" };
  }
  if (d.status === "received") return { tone: "ours", label: "Processing" };
  return null;
}

/**
 * The expiry countdown on the DATE CELL of a live document: past, today and
 * the next 90 days read the shared ladder and are urgent; beyond 90 days the
 * cell stays plain. A void document shows its date and no countdown.
 */
function expiryCountdown(v: DocumentValidity): {
  label?: string;
  urgent: boolean;
} {
  if (!v.live || v.daysLeft === null) return { urgent: false };
  if (v.daysLeft <= URGENT_WINDOW_DAYS)
    return { label: expiryWording(v.daysLeft), urgent: true };
  return { urgent: false };
}

/** Holding count: a live, dated document with 90 days or fewer left. */
function isExpiringSoon(d: ClientDocument, day: string): boolean {
  const v = documentValidity(d, day);
  return v.live && v.daysLeft !== null && v.daysLeft <= URGENT_WINDOW_DAYS;
}

/** Nearest expiry first; documents without one sort last. */
function byExpiryUrgency(a: ClientDocument, b: ClientDocument): number {
  const rank = (d: ClientDocument) => {
    const key = parseCalendarDate(d.expiry_date);
    return key ? dayNumber(key) : Number.POSITIVE_INFINITY;
  };
  return rank(a) - rank(b);
}

/**
 * `snake_case` → `Title Case`, matching the old component's own
 * normalisation (`OLD:279`: `.replace(/_/g, " ")` + a CSS `capitalize`
 * utility) so the restored type label reads exactly as it used to.
 */
function formatDocumentType(type: string): string {
  return type.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * The old component's per-category header carried its own doc count and
 * urgent-count simultaneously for every category (`OLD:211-213`,
 * `:214-219`); collapsing the headers into filter pills (this restyle) took
 * both readings with it. Restored onto the pill's own label, using the same
 * `expiryCountdown` the aggregate above uses — so a category never disagrees
 * with the total.
 */
function categoryPillLabel(
  label: string,
  total: number,
  expiringSoon: number,
): string {
  return expiringSoon > 0
    ? `${label} · ${total} · ${expiringSoon} soon`
    : `${label} · ${total}`;
}

export function DocumentsTab({
  clientId,
  documents,
  documentsByCategory,
  formatDate,
  onAddClick,
  onEditClick,
  onRefresh,
  initialGroup = "all",
  day: dayProp,
  clientFolderId,
  companyLinks,
}: {
  clientId: number;
  /** Every row the tab lists: the live general documents plus every void
   *  document of the profile, of whatever kind. */
  documents: ClientDocument[];
  /** The LIVE general documents, by category. */
  documentsByCategory: Record<string, ClientDocument[]>;
  formatDate: (d: string) => string;
  onAddClick: () => void;
  onEditClick: (doc: ClientDocument) => void;
  onRefresh: () => void;
  /** The group shown first: "removed" when a signpost sent the reader here. */
  initialGroup?: string;
  /** The card's day (`YYYY-MM-DD`, the payload's `as_of`). */
  day?: string;
  /** The client's Drive folder, for the Tax filter's Drive panel. */
  clientFolderId?: string | null;
  /** The linked companies, for the Tax filter's Drive panel. */
  companyLinks?: readonly ClientCompanyLink[];
}) {
  const day = resolveCardDay(dayProp);
  const [activeCategory, setActiveCategory] = useState<string>(initialGroup);

  // Archive, never delete: DELETE without ?permanent sets is_archived (row and
  // Drive file kept). Same confirm-toast pattern as ImmigrationTab.
  const handleArchive = (docId: number, name: string) => {
    toast(`Archive document "${name || "Document"}"?`, {
      action: {
        label: "Archive",
        onClick: async () => {
          try {
            await api.crm.deleteDocument(clientId, docId);
            toast.success("Document archived");
            onRefresh();
          } catch (err) {
            toast.error("Error", { description: (err as Error).message });
          }
        },
      },
      cancel: { label: "Cancel", onClick: () => toast.dismiss() },
    });
  };

  const sortedCategories = useMemo(
    () =>
      Object.keys(documentsByCategory).sort(
        (a, b) => CATEGORY_ORDER.indexOf(a) - CATEGORY_ORDER.indexOf(b),
      ),
    [documentsByCategory],
  );

  // Reverse lookup so a flat ("All") row still knows its own category —
  // `documentsByCategory` is the single source of truth for that grouping,
  // never `doc.document_category` alone (a doc can be bucketed under "other"
  // without that field set).
  const categoryByDocId = useMemo(() => {
    const map = new Map<number, string>();
    for (const [cat, docs] of Object.entries(documentsByCategory)) {
      for (const d of docs) map.set(d.id, cat);
    }
    return map;
  }, [documentsByCategory]);

  const voidDocuments = useMemo(
    () => documents.filter((d) => !documentValidity(d, day).live),
    [documents, day],
  );

  const filteredDocuments = useMemo(() => {
    const base =
      activeCategory === "all"
        ? documents
        : activeCategory === REMOVED_GROUP
          ? voidDocuments
          : (documentsByCategory[activeCategory] ?? []);
    return [...base].sort(byExpiryUrgency);
  }, [activeCategory, documents, documentsByCategory, voidDocuments]);

  // Holding count across ALL listed documents regardless of the active
  // filter: only live, dated documents count as "expiring soon".
  const totalExpiringSoon = useMemo(
    () => documents.filter((d) => isExpiringSoon(d, day)).length,
    [documents, day],
  );

  // Per-category total + expiring-soon, for the filter pill label — same
  // `isExpiringSoon` as `totalExpiringSoon` above.
  const categoryStats = useMemo(() => {
    const stats = new Map<string, { total: number; expiringSoon: number }>();
    for (const [cat, docs] of Object.entries(documentsByCategory)) {
      stats.set(cat, {
        total: docs.length,
        expiringSoon: docs.filter((d) => isExpiringSoon(d, day)).length,
      });
    }
    return stats;
  }, [documentsByCategory, day]);

  if (documents.length === 0) {
    return (
      <EmptyState
        action={
          <div className="flex flex-col items-start gap-3">
            <p className="text-sm text-[var(--tx-secondary)]">
              Upload passport, visa, or company documents
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={onAddClick}
              className="gap-2"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Add document
            </Button>
          </div>
        }
      >
        No documents on file yet.
      </EmptyState>
    );
  }

  return (
    <div data-client-id={clientId}>
      <DeskStrip
        count={filteredDocuments.length}
        countLabel="Documents"
        filters={
          <>
            <StatePill
              tone="wait"
              label={categoryPillLabel(
                "All",
                documents.length,
                totalExpiringSoon,
              )}
              pressed={activeCategory === "all"}
              onClick={() => setActiveCategory("all")}
            />
            {sortedCategories.map((cat) => {
              const stats = categoryStats.get(cat) ?? {
                total: 0,
                expiringSoon: 0,
              };
              return (
                <StatePill
                  key={cat}
                  tone="wait"
                  label={categoryPillLabel(
                    CATEGORY_LABELS[cat] || cat,
                    stats.total,
                    stats.expiringSoon,
                  )}
                  pressed={activeCategory === cat}
                  onClick={() => setActiveCategory(cat)}
                />
              );
            })}
            {voidDocuments.length > 0 && (
              <StatePill
                tone="wait"
                label={`Removed or rejected · ${voidDocuments.length}`}
                pressed={activeCategory === REMOVED_GROUP}
                onClick={() => setActiveCategory(REMOVED_GROUP)}
              />
            )}
          </>
        }
        right={
          <Button
            size="sm"
            variant="outline"
            onClick={onAddClick}
            className="gap-2"
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
            Add document
          </Button>
        }
        className="mb-2"
      />

      {totalExpiringSoon > 0 && (
        <p
          className="mb-2 px-0.5 text-[11px]"
          style={{ color: "var(--state-warning)" }}
        >
          {totalExpiringSoon} expiring soon
        </p>
      )}

      <HairlineGrid
        cols={COLS}
        className="max-sm:[&_.grid]:!grid-cols-[minmax(0,1fr)_auto]"
      >
        <HairlineHead>
          <span>Document</span>
          <span className="max-sm:hidden">Status</span>
          <span className="max-sm:hidden">Expires</span>
          <span aria-hidden="true" />
        </HairlineHead>
        <HairlineBody>
          {filteredDocuments.map((d) => {
            const cat =
              categoryByDocId.get(d.id) ?? d.document_category ?? "other";
            const catLabel = CATEGORY_LABELS[cat] || cat;
            const displayName = d.file_name || d.document_type;
            const typeLabel = formatDocumentType(d.document_type);
            const openUrl = getDocumentOpenUrl(d);
            const validity = documentValidity(d, day);
            const pillState = documentStatusPill(d, validity);
            const countdown = expiryCountdown(validity);
            const expiresLabel = validity.expiry
              ? formatDate(validity.expiry)
              : "—";
            const expiresColor = countdown.urgent
              ? "var(--state-warning)"
              : "var(--tx-pure)";
            const uploadedLabel = d.created_at
              ? formatDate(d.created_at)
              : undefined;

            return (
              <HairlineRow key={d.id}>
                <div className="min-w-0 px-2.5">
                  <CellStack
                    primary={
                      openUrl ? (
                        <a
                          href={openUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          aria-label={`Open ${displayName}`}
                          className={cn("rounded hover:underline", FOCUS)}
                        >
                          {displayName}
                        </a>
                      ) : (
                        displayName
                      )
                    }
                    secondary={
                      uploadedLabel
                        ? `${typeLabel} · ${catLabel} · ${uploadedLabel}`
                        : `${typeLabel} · ${catLabel}`
                    }
                  />
                  {(d.client_visible === false ||
                    d.client_visible === null) && (
                    <span className="mt-0.5 block text-[11px] text-[var(--tx-secondary)]">
                      Hidden from the client
                    </span>
                  )}
                  {d.deleted_at && (
                    <span className="mt-0.5 block text-[11px] text-[var(--tx-secondary)]">
                      Removed by the client on {formatDate(d.deleted_at)} —
                      restorable by them for 30 days
                    </span>
                  )}
                  <span className="mt-1 flex flex-col gap-0.5 text-[11px] text-[var(--tx-secondary)] sm:hidden">
                    {pillState ? (
                      <StatePill
                        tone={pillState.tone}
                        label={pillState.label}
                      />
                    ) : (
                      <span>—</span>
                    )}
                    <span style={{ color: expiresColor, ...TABULAR }}>
                      {expiresLabel}
                      {countdown.label ? ` · ${countdown.label}` : ""}
                    </span>
                  </span>
                </div>

                <span className="max-sm:hidden">
                  {pillState ? (
                    <StatePill tone={pillState.tone} label={pillState.label} />
                  ) : (
                    <span className="text-[var(--tx-secondary)]">—</span>
                  )}
                </span>

                <div className="max-sm:hidden flex flex-col justify-center gap-px">
                  <span style={{ color: expiresColor, ...TABULAR }}>
                    {expiresLabel}
                  </span>
                  {countdown.label && (
                    <span
                      className="text-[11px]"
                      style={{ color: expiresColor }}
                    >
                      {countdown.label}
                    </span>
                  )}
                </div>

                <div className="flex items-center justify-end gap-1 px-2.5">
                  {openUrl ? (
                    <a
                      href={openUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`View ${displayName}`}
                      className={cn(
                        "inline-flex h-7 w-7 items-center justify-center border border-transparent text-[var(--tx-secondary)] hover:border-[var(--line-control)] hover:text-[var(--tx-pure)]",
                        FOCUS,
                      )}
                    >
                      <Eye className="h-3.5 w-3.5" aria-hidden="true" />
                    </a>
                  ) : (
                    <span
                      className="text-[10px] leading-tight text-[var(--tx-secondary)]"
                      title="File tidak tersedia"
                    >
                      File tidak tersedia
                    </span>
                  )}
                  <button
                    type="button"
                    aria-label={`Edit ${displayName}`}
                    onClick={() => onEditClick(d)}
                    className={cn(
                      "inline-flex h-7 w-7 items-center justify-center border border-transparent text-[var(--tx-secondary)] hover:border-[var(--line-control)] hover:text-[var(--tx-pure)]",
                      FOCUS,
                    )}
                  >
                    <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    aria-label={`Archive ${displayName}`}
                    onClick={() => handleArchive(d.id, displayName)}
                    className={cn(
                      "inline-flex h-7 w-7 items-center justify-center border border-transparent text-[var(--tx-secondary)] hover:border-[var(--line-control)] hover:text-[var(--tx-pure)]",
                      FOCUS,
                    )}
                  >
                    <Archive className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </div>
              </HairlineRow>
            );
          })}
        </HairlineBody>
      </HairlineGrid>

      {/* Tax filter: the same read-only Drive tax folders as Tax › Tax
          Documents (#345), mounted only while the filter is on so the other
          filters make no Drive calls. */}
      {activeCategory === TAX_GROUP && (
        <section aria-label="Tax Documents" className="mt-6">
          <h3 className={MICRO_LABEL}>Tax Documents</h3>
          <TaxDocumentsSection
            clientFolderId={clientFolderId}
            companyLinks={companyLinks}
          />
        </section>
      )}
    </div>
  );
}

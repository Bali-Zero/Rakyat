"use client";

/**
 * Related-subjects registry — staff page (kita workspace).
 *
 * Backend: apps/backend-rag/backend/app/routers/crm_related_registry_read.py,
 * `GET /api/crm/related-registry` and `GET /api/crm/related-registry/{id}`,
 * and crm_related_link_reviews.py, `POST /api/crm/related-registry/link-reviews`
 * (a CRM admin confirms a proposed client link or marks it not related),
 * CRM admins only. A non-admin gets a plain 403, shown as "Admin only." and no
 * table. The page has no role check of its own: the server's answer decides.
 *
 * Answers that may update the page: only the latest list request for the
 * current search, kind, client-link filter and offset, and only the detail request for the
 * subject currently selected. A superseded answer, error included, is ignored
 * (the effect cleanup flips a per-request flag). A saved decision re-reads the
 * detail through a refresh counter that supersedes every earlier detail request.
 * Saves in flight are tracked per subject, so a subject's buttons stay disabled
 * while its own save is pending, whichever subjects were opened meanwhile. A
 * save that completes re-reads only the subject on screen if it is the saved
 * one, and shows its error only if the admin never left that subject since
 * starting it (a selection epoch, bumped on every change of subject).
 * The search text is sent only
 * as the query key `q` and is never logged.
 */

import Link from "next/link";
import { Fragment, useEffect, useRef, useState } from "react";

import {
  getRegistrySubject,
  listRegistry,
  reviewRegistryLink,
  type RegistryDetail,
  type RegistryKind,
  type RegistryLink,
  type RegistryLinked,
  type RegistryReviewRelation,
  type RegistryRow,
} from "@/lib/api/related-registry/related-registry";
import { cn } from "@/lib/utils";
import {
  DeskStrip,
  EmptyState,
  Field,
  FOCUS,
  Masthead,
  Notice,
  SERIF_SECTION,
  StatePill,
} from "@/components/workspace/r19";

import { describeError } from "../../obligations/components/describe-error";

const LIMIT = 50;
const DEBOUNCE_MS = 300;
const MAX_LISTED_IDENTIFIERS = 3;

type KindFilter = "all" | RegistryKind;

const KIND_OPTIONS: ReadonlyArray<{ value: KindFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "person", label: "People" },
  { value: "company", label: "Companies" },
];

type LinkedFilter = "all" | RegistryLinked;

const LINKED_OPTIONS: ReadonlyArray<{ value: LinkedFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "true", label: "Linked" },
  { value: "false", label: "Not linked" },
];

interface ListQuery {
  q: string;
  kind: KindFilter;
  linked: LinkedFilter;
  offset: number;
}

/** Bytes in the largest unit that keeps the number under 1024. */
function humanSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
}

function driveUrl(fileId: string): string {
  return `https://drive.google.com/file/d/${encodeURIComponent(fileId)}/view`;
}

/** Sources whose name was read from a document: a lead for a person, not a fact. */
const OCR_MARKS: Readonly<Record<string, string>> = {
  ocr_corroborated: "OCR",
  ocr_single: "OCR, one file",
};
const OCR_TITLE =
  "This name was read from a document by OCR. It is not verified.";

const SOURCE_LABELS: Readonly<Record<string, string>> = {
  manual: "entered by hand",
  mrz_corrected: "corrected from the passport MRZ",
  registration: "registration",
  ocr_corroborated: "OCR",
  ocr_single: "OCR, one file",
};

/** How a link was proposed: the producer's basis, shown as a short label. */
const BASIS_LABELS: Readonly<Record<string, string>> = {
  drive_folder: "Drive folder",
  whatsapp_phone: "WhatsApp phone",
  identity: "Identity",
  manual: "Entered by hand",
};

/**
 * What each basis asserts, in the producer's words: where a link comes from, never how the
 * subject and the client are related. The order is the order of the legend.
 */
const BASIS_MEANINGS: Readonly<Record<string, string>> = {
  drive_folder: "files of this subject sit in the client's Drive folder.",
  whatsapp_phone:
    "files of this subject arrived in the WhatsApp thread of the client's phone.",
  identity:
    "an identifier or identity datum of this subject equals one held by the client.",
  manual: "a staff member entered this link.",
};

/** How a merge was decided: the history row's basis, shown as a short label. */
const MERGE_BASIS_LABELS: Readonly<Record<string, string>> = {
  permit_stamp: "Permit stamp",
  same_identity: "Same identity",
  manual: "Entered by hand",
};

/** What a detachment basis asserts; an unknown basis shows no meaning text. */
const DETACH_BASIS_MEANINGS: Readonly<Record<string, string>> = {
  nik_birth_date:
    "the national id number (NIK) encodes a birth date that differs from this person's, so the number belongs to someone else",
};

const RELATION_OPTIONS: ReadonlyArray<{
  value: RegistryReviewRelation;
  label: string;
}> = [
  { value: "self", label: "The client themself" },
  { value: "spouse", label: "Spouse" },
  { value: "child", label: "Child" },
  { value: "employee", label: "Employee" },
  { value: "other", label: "Other" },
];

const RELATION_LABELS: Readonly<Record<string, string>> = Object.fromEntries(
  RELATION_OPTIONS.map((o) => [o.value, o.label]),
);

/** What each suggestion basis says agrees with the client card. */
const SUGGESTION_WORDS: Readonly<Record<string, string>> = {
  passport_equal: "the passport number",
  passport_one_char_apart: "the passport number (one character apart)",
  nik_encodes_client_birth_date: "the birth date in the NIK",
  birth_date_equal: "the birth date",
};

const SAVE_ERROR = "Could not save the decision.";

function preselectionCaption(basis: readonly string[]): string {
  const words = basis.flatMap((b) => SUGGESTION_WORDS[b] ?? []);
  const joined =
    words.length <= 1
      ? words.join("")
      : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
  return `Preselected: the name${joined ? ` and ${joined}` : ""} agree${joined ? "" : "s"} with the client card.`;
}

function statusLine(review: RegistryLink["review"]): string {
  if (!review) return "Proposed, not confirmed.";
  const when = `${review.decided_by}, ${review.decided_at.slice(0, 10)}`;
  if (review.decision === "rejected") return `Not related — ${when}`;
  const label = review.relation
    ? (RELATION_LABELS[review.relation] ?? "—")
    : "—";
  return `Confirmed: ${label} — ${when}`;
}

/**
 * The decision control of one client: a status line, a relation select and two
 * buttons. The select starts from the effective confirmation, else from the
 * server's `self` suggestion when there is no review at all; a rejection is
 * never contradicted by a preselection. Remounted (keyed) on every new answer.
 */
function ReviewControl({
  link,
  busy,
  onDecide,
}: {
  link: RegistryLink;
  busy: boolean;
  onDecide: (
    clientId: number,
    decision: "confirmed" | "rejected",
    relation?: RegistryReviewRelation,
  ) => void;
}) {
  const review = link.review ?? null;
  const preselected =
    !review && link.suggested_relation === "self"
      ? ("self" as RegistryReviewRelation)
      : null;
  const initial: RegistryReviewRelation | null =
    review?.decision === "confirmed"
      ? ((review.relation as RegistryReviewRelation | null) ?? null)
      : preselected;
  const [choice, setChoice] = useState<RegistryReviewRelation | "">(
    initial ?? "",
  );
  const selectId = `review-relation-${link.client_id}`;
  return (
    <div data-testid="registry-review" className="mb-2 mt-1">
      <p
        data-testid="registry-review-status"
        className="text-[12px] text-[var(--tx-secondary)]"
      >
        {statusLine(review)}
      </p>
      <label htmlFor={selectId} className="sr-only">
        {`Relation of the subject to client #${link.client_id}`}
      </label>
      <select
        id={selectId}
        value={choice}
        disabled={busy}
        onChange={(e) => setChoice(e.target.value as RegistryReviewRelation)}
        className={cn(
          "min-h-11 border border-[var(--line-control)] bg-transparent px-2 text-[12px] text-[var(--tx-pure)]",
          FOCUS,
        )}
      >
        <option value="" disabled>
          Relation…
        </option>
        {RELATION_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>{" "}
      <button
        type="button"
        disabled={busy || choice === ""}
        onClick={() => choice && onDecide(link.client_id, "confirmed", choice)}
        className={BUTTON}
      >
        Confirm
      </button>{" "}
      <button
        type="button"
        disabled={busy}
        onClick={() => onDecide(link.client_id, "rejected")}
        className={BUTTON}
      >
        Not related
      </button>
      {preselected && choice === preselected && (
        <p
          data-testid="registry-review-caption"
          className="text-[12px] text-[var(--tx-secondary)]"
        >
          {preselectionCaption(link.suggestion_basis ?? [])}
        </p>
      )}
    </div>
  );
}

function detachIdType(idType: string): string {
  return idType.toUpperCase();
}

function documentsMoved(count: number): string {
  return `${count} ${count === 1 ? "document" : "documents"} moved.`;
}

/** The mark on an item that belongs to a record merged into the opened subject. */
function FromMark({ id }: { id?: number | null }) {
  if (id === null || id === undefined) return null;
  return (
    <span
      data-testid="from-mark"
      className="ml-2 align-middle text-[11px] text-[var(--tx-secondary)]"
    >
      {`from merged record #${id}`}
    </span>
  );
}

/** The mark on a list row of a subject that has at least one proposed client link. */
function ClientMark({ count }: { count?: number | null }) {
  if (!count || count <= 0) return null;
  return (
    <span
      data-testid="client-mark"
      title="This subject is proposed as linked to a CRM client. Not confirmed."
      className="ml-2 border border-[var(--line-control)] px-1.5 py-0.5 align-middle text-[10px] font-[650] uppercase tracking-[0.08em] text-[var(--tx-secondary)]"
    >
      Client
    </span>
  );
}

/** The mark beside a name read by OCR; nothing for any other source. */
function NameMark({ source }: { source?: string | null }) {
  const label = source ? OCR_MARKS[source] : undefined;
  if (!label) return null;
  return (
    <span
      data-testid="name-mark"
      title={OCR_TITLE}
      className="ml-2 border border-[var(--line-control)] px-1.5 py-0.5 align-middle text-[10px] font-[650] uppercase tracking-[0.08em] text-[var(--tx-secondary)]"
    >
      {label}
    </span>
  );
}

const CELL = "px-3 py-2 align-top text-[13px] text-[var(--tx-pure)]";
const HEAD =
  "px-3 py-2 text-left text-[10px] font-[650] uppercase tracking-[0.12em] text-[var(--tx-secondary)]";
const BUTTON = cn(
  "inline-flex min-h-11 items-center border border-[var(--line-control)] bg-transparent px-3.5 text-[12px] font-[650] text-[var(--tx-pure)] hover:bg-[var(--bz-card-hover)] disabled:opacity-40",
  FOCUS,
);

export default function RelatedRegistryPage() {
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState<ListQuery>({
    q: "",
    kind: "all",
    linked: "all",
    offset: 0,
  });

  const [items, setItems] = useState<RegistryRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<RegistryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [refresh, setRefresh] = useState(0);
  const [pending, setPending] = useState<ReadonlySet<number>>(
    () => new Set<number>(),
  );
  const [saveError, setSaveError] = useState(false);
  const selectedRef = useRef<number | null>(null);
  const selectionEpochRef = useRef(0);
  const loadedForRef = useRef<number | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const detailRef = useRef<HTMLElement | null>(null);

  // Selecting a subject brings its detail section into view.
  useEffect(() => {
    if (selectedId === null) return;
    detailRef.current?.scrollIntoView?.({ block: "start" });
  }, [selectedId]);

  // Debounce: the effective search follows the input 300 ms after the last
  // keystroke, only for an empty or 2+ character trimmed text. One character
  // cancels the pending timer and leaves the effective search where it was.
  useEffect(() => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    const trimmed = searchInput.trim();
    if (trimmed.length === 1) return;
    debounceRef.current = setTimeout(() => {
      debounceRef.current = null;
      setQuery((prev) =>
        prev.q === trimmed ? prev : { ...prev, q: trimmed, offset: 0 },
      );
    }, DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
    };
  }, [searchInput]);

  // List: one request per (search, kind, offset); an older answer is dropped.
  useEffect(() => {
    let current = true;
    setLoading(true);
    setListError(null);
    listRegistry({
      q: query.q || undefined,
      kind: query.kind === "all" ? undefined : query.kind,
      linked: query.linked === "all" ? undefined : query.linked,
      limit: LIMIT,
      offset: query.offset,
    })
      .then((res) => {
        if (!current) return;
        setItems(res.items ?? []);
        setTotal(res.total ?? 0);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (!current) return;
        setListError(describeError(e, "Could not load the registry."));
        setItems([]);
        setTotal(0);
        setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [query]);

  // Detail: only the subject currently selected may fill the panel. A refresh
  // (after a saved decision) re-reads the same subject without blanking the
  // panel and supersedes every earlier detail request.
  useEffect(() => {
    if (selectedRef.current !== selectedId) selectionEpochRef.current += 1;
    selectedRef.current = selectedId;
    setSaveError(false);
    if (selectedId === null) {
      loadedForRef.current = null;
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    let current = true;
    if (loadedForRef.current !== selectedId) {
      loadedForRef.current = selectedId;
      setDetail(null);
    }
    setDetailError(null);
    setDetailLoading(true);
    getRegistrySubject(selectedId)
      .then((res) => {
        if (!current) return;
        setDetail(res);
        setDetailLoading(false);
      })
      .catch((e: unknown) => {
        if (!current) return;
        setDetailError(describeError(e, "Could not load this subject."));
        setDetailLoading(false);
      });
    return () => {
      current = false;
    };
  }, [selectedId, refresh]);

  const decide = (
    subjectId: number,
    clientId: number,
    decision: "confirmed" | "rejected",
    relation?: RegistryReviewRelation,
  ) => {
    const epoch = selectionEpochRef.current;
    setPending((prev) => new Set(prev).add(subjectId));
    setSaveError(false);
    const body =
      decision === "confirmed" && relation
        ? {
            subject_id: subjectId,
            client_id: clientId,
            decision,
            relation,
          }
        : {
            subject_id: subjectId,
            client_id: clientId,
            decision: "rejected" as const,
          };
    reviewRegistryLink(body)
      .then(() => {
        if (selectedRef.current !== subjectId) return;
        setRefresh((n) => n + 1);
      })
      .catch(() => {
        if (selectionEpochRef.current !== epoch) return;
        setSaveError(true);
      })
      .finally(() => {
        setPending((prev) => {
          if (!prev.has(subjectId)) return prev;
          const next = new Set(prev);
          next.delete(subjectId);
          return next;
        });
      });
  };

  const setKind = (kind: KindFilter) =>
    setQuery((prev) =>
      prev.kind === kind ? prev : { ...prev, kind, offset: 0 },
    );

  const setLinked = (linked: LinkedFilter) =>
    setQuery((prev) =>
      prev.linked === linked ? prev : { ...prev, linked, offset: 0 },
    );

  const shownLinks = detail?.links ?? [];
  const linksTotal = detail?.links_total ?? shownLinks.length;
  const shownBases = Object.keys(BASIS_MEANINGS).filter((basis) =>
    shownLinks.some((link) => link.basis === basis),
  );
  const mergedFrom = detail?.merged_from ?? [];
  const mergedInto = detail?.merged_into ?? null;
  const detachedFrom = detail?.detached_from ?? [];
  const detachedInto = detail?.detached_into ?? [];
  const anyFileCount = shownLinks.some(
    (link) => typeof link.files === "number" && link.files >= 1,
  );
  const shownNames = detail?.names ?? [];
  const namesTotal = detail?.names_total ?? shownNames.length;

  const pageStart = total === 0 ? 0 : query.offset + 1;
  const pageEnd = Math.min(query.offset + LIMIT, total);

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <Masthead
        eyebrow="Admin"
        title="Related subjects registry"
        subtitle="People and companies related to clients, with their identifiers and documents by reference. Read only."
        className="mb-6"
      />

      <DeskStrip
        className="mb-4"
        filters={
          <div className="flex min-w-0 items-center gap-4 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            <div
              role="group"
              aria-label="Subject kind"
              className="flex items-center gap-1.5"
            >
              {KIND_OPTIONS.map((option) => (
                <StatePill
                  key={option.value}
                  tone="wait"
                  label={option.label}
                  pressed={query.kind === option.value}
                  onClick={() => setKind(option.value)}
                />
              ))}
            </div>
            <div
              role="group"
              aria-label="Client link"
              className="flex items-center gap-1.5"
            >
              {LINKED_OPTIONS.map((option) => (
                <StatePill
                  key={option.value}
                  tone="wait"
                  label={option.label}
                  pressed={query.linked === option.value}
                  onClick={() => setLinked(option.value)}
                />
              ))}
            </div>
          </div>
        }
        right={
          <Field
            id="registry-search"
            label="Search"
            type="search"
            autoComplete="off"
            placeholder="name or identifier"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            wrapperClassName="w-56"
          />
        }
      />

      {listError && (
        <Notice tone="you" role="alert" className="mb-4">
          {listError}
        </Notice>
      )}

      {loading ? (
        <p style={{ color: "var(--bz-text-3)" }}>Loading…</p>
      ) : !listError && items.length === 0 ? (
        <EmptyState>No subjects found.</EmptyState>
      ) : (
        !listError && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-[var(--bz-border)]">
                  <th className={HEAD}>Name</th>
                  <th className={HEAD}>Kind</th>
                  <th className={HEAD}>Identifiers</th>
                  <th className={HEAD}>Documents</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => {
                  const shown = row.identifiers.slice(
                    0,
                    MAX_LISTED_IDENTIFIERS,
                  );
                  const more = row.identifiers.length - shown.length;
                  return (
                    <tr
                      key={row.id}
                      data-testid="registry-row"
                      onClick={() => setSelectedId(row.id)}
                      className={cn(
                        "cursor-pointer border-b border-[var(--bz-border)] hover:bg-[var(--bz-card-hover)]",
                        selectedId === row.id && "bg-[var(--bz-card-hover)]",
                      )}
                    >
                      <td className={CELL}>
                        {row.display_name ?? "—"}
                        <NameMark source={row.name_source} />
                        <ClientMark count={row.links_count} />
                      </td>
                      <td className={CELL}>{row.kind}</td>
                      <td className={CELL}>
                        {shown.map((identifier) => (
                          <div
                            key={`${identifier.type}:${identifier.value}`}
                          >{`${identifier.type.toUpperCase()} ${identifier.value}`}</div>
                        ))}
                        {more > 0 && <div>{`+${more}`}</div>}
                      </td>
                      <td className={CELL}>{row.document_count}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}

      {!listError && total > 0 && (
        <div
          className="mt-3 flex items-center justify-between text-xs"
          style={{ color: "var(--bz-text-3)" }}
        >
          <span>
            Showing {pageStart}-{pageEnd} of {total}
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={query.offset === 0 || loading}
              onClick={() =>
                setQuery((prev) => ({
                  ...prev,
                  offset: Math.max(0, prev.offset - LIMIT),
                }))
              }
              className={BUTTON}
            >
              Previous
            </button>
            <button
              type="button"
              disabled={query.offset + LIMIT >= total || loading}
              onClick={() =>
                setQuery((prev) => ({ ...prev, offset: prev.offset + LIMIT }))
              }
              className={BUTTON}
            >
              Next
            </button>
          </div>
        </div>
      )}

      {selectedId !== null && (
        <section
          ref={detailRef}
          className="mt-8 border-t border-[var(--bz-border)] pt-4"
          aria-label="Subject detail"
        >
          {detailLoading && (
            <p style={{ color: "var(--bz-text-3)" }}>Loading subject…</p>
          )}
          {detailError && (
            <Notice tone="you" role="alert">
              {detailError}
            </Notice>
          )}
          {detail && (
            <div className="space-y-4">
              <h2
                className="text-[19px] leading-[1.2] tracking-[-0.02em] text-[var(--tx-pure)]"
                style={SERIF_SECTION}
              >
                {detail.display_name ?? `Unnamed subject #${detail.id}`}
                <NameMark source={detail.name_source} />
              </h2>
              {mergedInto !== null && (
                <Notice tone="wait" role="status">
                  {`This record was merged into subject #${mergedInto}. `}
                  <button
                    type="button"
                    data-testid="open-survivor"
                    onClick={() => setSelectedId(mergedInto)}
                    className={cn("text-[var(--bz-copper)] underline", FOCUS)}
                  >
                    {`Open subject #${mergedInto}`}
                  </button>
                </Notice>
              )}
              {mergedFrom.length > 0 && (
                <div data-testid="registry-merged-from">
                  <p className="text-[13px] text-[var(--tx-secondary)]">
                    {mergedFrom.length === 1
                      ? "1 record merged into this one"
                      : `${mergedFrom.length} records merged into this one`}
                  </p>
                  <ul className="text-[13px] text-[var(--tx-pure)]">
                    {mergedFrom.map((merged) => (
                      <li
                        key={merged.subject_id}
                        data-testid="registry-merged-record"
                      >
                        {[
                          `#${merged.subject_id}`,
                          merged.basis
                            ? (MERGE_BASIS_LABELS[merged.basis] ?? null)
                            : null,
                          merged.merged_at
                            ? merged.merged_at.slice(0, 10)
                            : null,
                        ]
                          .filter((part) => part !== null)
                          .join(" · ")}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {(detachedFrom.length > 0 || detachedInto.length > 0) && (
                <div data-testid="registry-detachments">
                  <ul className="text-[13px] text-[var(--tx-pure)]">
                    {detachedFrom.map((row, index) => (
                      <li
                        key={`from:${index}:${row.subject_id}`}
                        data-testid="registry-detached-from"
                      >
                        {`Split off subject #${row.subject_id} on ${row.detached_at.slice(0, 10)}: ${detachIdType(row.id_type)}${
                          DETACH_BASIS_MEANINGS[row.basis]
                            ? `, ${DETACH_BASIS_MEANINGS[row.basis]}`
                            : ""
                        }. ${documentsMoved(row.documents_moved)} `}
                        <button
                          type="button"
                          data-testid="open-detached-source"
                          onClick={() => setSelectedId(row.subject_id)}
                          className={cn(
                            "text-[var(--bz-copper)] underline",
                            FOCUS,
                          )}
                        >
                          {`Open subject #${row.subject_id}`}
                        </button>
                        <FromMark id={row.from_subject_id} />
                      </li>
                    ))}
                    {detachedInto.map((row, index) => (
                      <li
                        key={`into:${index}:${row.subject_id}`}
                        data-testid="registry-detached-into"
                      >
                        {`Subject #${row.subject_id} was split off this record on ${row.detached_at.slice(0, 10)}: ${detachIdType(row.id_type)}${
                          DETACH_BASIS_MEANINGS[row.basis]
                            ? `, ${DETACH_BASIS_MEANINGS[row.basis]}`
                            : ""
                        }. ${documentsMoved(row.documents_moved)} `}
                        <button
                          type="button"
                          data-testid="open-detached-record"
                          onClick={() => setSelectedId(row.subject_id)}
                          className={cn(
                            "text-[var(--bz-copper)] underline",
                            FOCUS,
                          )}
                        >
                          {`Open subject #${row.subject_id}`}
                        </button>
                        <FromMark id={row.from_subject_id} />
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px] text-[var(--tx-pure)]">
                <dt className="text-[var(--tx-secondary)]">Kind</dt>
                <dd>{detail.kind}</dd>
                {detail.date_of_birth && (
                  <>
                    <dt className="text-[var(--tx-secondary)]">
                      Date of birth
                    </dt>
                    <dd>{detail.date_of_birth}</dd>
                  </>
                )}
                {detail.nationality && (
                  <>
                    <dt className="text-[var(--tx-secondary)]">Nationality</dt>
                    <dd>{detail.nationality}</dd>
                  </>
                )}
              </dl>

              <div>
                <h3 className={HEAD}>Names</h3>
                {shownNames.length === 0 ? (
                  <p className="text-[13px] text-[var(--tx-secondary)]">
                    No names.
                  </p>
                ) : (
                  <ul className="text-[13px] text-[var(--tx-pure)]">
                    {shownNames.map((entry, index) => (
                      <li
                        key={`${index}:${entry.source}:${entry.name}`}
                        data-testid="registry-name"
                      >
                        {entry.name} · {SOURCE_LABELS[entry.source] ?? "—"} ·{" "}
                        {entry.created_at.slice(0, 10)}
                        <FromMark id={entry.from_subject_id} />
                      </li>
                    ))}
                  </ul>
                )}
                {namesTotal > shownNames.length && (
                  <p
                    data-testid="registry-names-truncated"
                    className="text-[13px] text-[var(--tx-secondary)]"
                  >
                    {`Showing ${shownNames.length} of ${namesTotal} names.`}
                  </p>
                )}
              </div>

              {shownLinks.length > 0 && (
                <div data-testid="registry-clients">
                  <h3 className={HEAD}>Clients</h3>
                  <p className="text-[13px] text-[var(--tx-secondary)]">
                    Proposed from documents. A CRM admin confirms each link
                    below.
                  </p>
                  {saveError && (
                    <Notice tone="you" role="alert">
                      {SAVE_ERROR}
                    </Notice>
                  )}
                  {anyFileCount && (
                    <p
                      data-testid="registry-files-caveat"
                      className="text-[13px] text-[var(--tx-secondary)]"
                    >
                      The file count is how many files the proposal was read
                      from; one file is the weakest support.
                    </p>
                  )}
                  {shownBases.length > 0 && (
                    <div
                      data-testid="registry-basis-legend"
                      className="text-[12px] text-[var(--tx-secondary)]"
                    >
                      <ul>
                        {shownBases.map((basis) => (
                          <li key={basis} data-testid="registry-basis-meaning">
                            {`${BASIS_LABELS[basis]}: ${BASIS_MEANINGS[basis]}`}
                          </li>
                        ))}
                      </ul>
                      <p data-testid="registry-basis-caveat">
                        A basis says where a link comes from, not how the
                        subject and the client are related.
                      </p>
                    </div>
                  )}
                  <ul className="text-[13px] text-[var(--tx-pure)]">
                    {shownLinks.map((link, index) => (
                      <Fragment
                        key={`${index}:${link.client_id}:${link.basis}`}
                      >
                        <li data-testid="registry-client">
                          {link.client_live ? (
                            <Link
                              href={`/clients/${link.client_id}`}
                              className={cn(
                                "text-[var(--bz-copper)] underline",
                                FOCUS,
                              )}
                            >
                              {link.client_name ?? `Client #${link.client_id}`}
                            </Link>
                          ) : (
                            <span className="text-[var(--tx-secondary)]">
                              Client no longer in the CRM
                            </span>
                          )}{" "}
                          · {BASIS_LABELS[link.basis] ?? "—"} ·{" "}
                          {link.created_at.slice(0, 10)}
                          {typeof link.files === "number" &&
                            link.files >= 1 && (
                              <span data-testid="registry-client-files">
                                {` · ${link.files} ${link.files === 1 ? "file" : "files"}`}
                              </span>
                            )}
                          <FromMark id={link.from_subject_id} />
                        </li>
                        {shownLinks.findIndex(
                          (l) => l.client_id === link.client_id,
                        ) === index && (
                          <li data-testid="registry-review-item">
                            <ReviewControl
                              key={`${detail.id}:${link.client_id}:${link.review?.decided_at ?? "none"}`}
                              link={link}
                              busy={pending.has(detail.id)}
                              onDecide={(clientId, decision, relation) =>
                                decide(detail.id, clientId, decision, relation)
                              }
                            />
                          </li>
                        )}
                      </Fragment>
                    ))}
                  </ul>
                  {linksTotal > shownLinks.length && (
                    <p
                      data-testid="registry-clients-truncated"
                      className="text-[13px] text-[var(--tx-secondary)]"
                    >
                      {`Showing ${shownLinks.length} of ${linksTotal} clients.`}
                    </p>
                  )}
                </div>
              )}

              <div>
                <h3 className={HEAD}>Identifiers</h3>
                <ul className="text-[13px] text-[var(--tx-pure)]">
                  {detail.identifiers.map((identifier) => (
                    <li key={`${identifier.type}:${identifier.value}`}>
                      {`${identifier.type.toUpperCase()} ${identifier.value}`}
                      <FromMark id={identifier.from_subject_id} />
                    </li>
                  ))}
                </ul>
              </div>

              <div>
                <h3 className={HEAD}>Documents</h3>
                {detail.documents.length === 0 ? (
                  <p className="text-[13px] text-[var(--tx-secondary)]">
                    No documents.
                  </p>
                ) : (
                  <ul className="space-y-2 text-[13px] text-[var(--tx-pure)]">
                    {detail.documents.map((doc) => (
                      <li
                        key={doc.sha256}
                        data-testid="registry-document"
                        className="border-b border-[var(--bz-border)] pb-2"
                      >
                        <div>
                          {doc.doc_type ?? "—"} · {doc.mime_type ?? "—"} ·{" "}
                          {humanSize(doc.byte_size)} · first seen{" "}
                          {doc.first_seen_on ?? "—"}
                          <FromMark id={doc.from_subject_id} />
                        </div>
                        {doc.drive_file_ids.map((fileId) => (
                          <div key={fileId}>
                            <a
                              href={driveUrl(fileId)}
                              target="_blank"
                              rel="noopener noreferrer"
                              className={cn(
                                "text-[var(--bz-copper)] underline",
                                FOCUS,
                              )}
                            >
                              {fileId}
                            </a>
                          </div>
                        ))}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

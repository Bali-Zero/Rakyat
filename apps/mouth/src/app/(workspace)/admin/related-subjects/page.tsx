"use client";

/**
 * Related-subjects registry — read-only staff page (kita workspace).
 *
 * Backend: apps/backend-rag/backend/app/routers/crm_related_registry_read.py,
 * `GET /api/crm/related-registry` and `GET /api/crm/related-registry/{id}`,
 * CRM admins only. A non-admin gets a plain 403, shown as "Admin only." and no
 * table. The page has no role check of its own: the server's answer decides.
 *
 * Answers that may update the page: only the latest list request for the
 * current search, kind, client-link filter and offset, and only the detail request for the
 * subject currently selected. A superseded answer, error included, is ignored
 * (the effect cleanup flips a per-request flag). The search text is sent only
 * as the query key `q` and is never logged.
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  getRegistrySubject,
  listRegistry,
  type RegistryDetail,
  type RegistryKind,
  type RegistryLinked,
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

  // Detail: only the subject currently selected may fill the panel.
  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    let current = true;
    setDetail(null);
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
  }, [selectedId]);

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
                    Proposed from documents, not confirmed.
                  </p>
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
                      <li
                        key={`${index}:${link.client_id}:${link.basis}`}
                        data-testid="registry-client"
                      >
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
                        <FromMark id={link.from_subject_id} />
                      </li>
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

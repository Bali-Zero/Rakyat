"use client";

import React from "react";
import {
  Globe,
  Plus,
  Edit2,
  Trash2,
  Download,
  RefreshCw,
  Upload,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { extractDriveFileId } from "./utils";
import { isImmigrationDocType } from "./immigration-doc-type";
import { permitDisplay } from "./permit-label";
import { COMPLETED_PILL, CURRENT_PILL } from "./permit-status-pills";
import { AiRecap } from "./AiRecap";
import {
  CellStack,
  FOCUS,
  HairlineBody,
  HairlineGrid,
  HairlineHead,
  HairlineRow,
  LedgerSection,
  Numeral,
} from "@/components/workspace/r19";
import { ValidityTrack } from "./ValidityTrack";
import {
  isSamePermit,
  isStayPermitDocument,
  pickCurrentVisa,
} from "./pickCurrentVisa";
import {
  URGENT_WINDOW_DAYS,
  dayNumber,
  documentValidity,
  expiryWording,
  isLive,
  isUrgent as isUrgentValidity,
  parseCalendarDate,
} from "./documentValidity";
import { RemovedSignpost } from "./RemovedSignpost";
import { resolveCardDay } from "./witaClock";

/**
 * Start Renewal: a renewable type, a LIVE document, a date, and 90 days or
 * fewer left (already expired included). A void document is never renewable.
 */
export function isRenewable(doc: ClientDocument, day: string): boolean {
  if (!isLive(doc)) return false;
  const { daysLeft } = documentValidity(doc, day);
  return (
    daysLeft !== null &&
    daysLeft <= URGENT_WINDOW_DAYS &&
    Boolean(
      doc.document_type?.toLowerCase().includes("kitas") ||
      doc.document_type?.toLowerCase().includes("kitap") ||
      doc.document_type?.toLowerCase().includes("visa"),
    )
  );
}

/**
 * Whether a visa-family document's expiry deserves the one kita warning
 * treatment. Restores the OLD chip's `alert_color` signal (server-driven,
 * independent of the date) alongside the date-math threshold, instead of
 * dropping it the way the first version of this panel silently did — a
 * "red"/"expired"/"yellow" `alert_color` is urgent even 91+ days out. The
 * server's `yellow` is a live signal same as `red`/`expired` (D-5's other
 * half; `constants.ts:20-22` routed it to a warning tone before this panel
 * existed). All three still route to the ONE `--state-warning` tone, never
 * the old per-bucket yellow/red hues (there is no fifth hue in this module).
 */
function isUrgent(doc: ClientDocument, day: string): boolean {
  return isUrgentValidity(
    documentValidity(doc, day),
    URGENT_WINDOW_DAYS,
    doc.alert_color,
  );
}

/** `.replace(/_/g, " ")` — the OLD card's normalisation, reused so the
 * permit panel and the history grid don't show a raw `e_voa`/`kitas_c317`. */
function formatDocType(type: string): string {
  return type.replace(/_/g, " ");
}

/** `permit_label` (resolved server-side from OCR/document_type, see
 * crm.types.ts) is the precise label when known, shown as `<code> · <name>`
 * without the taxonomy's qualifiers; `document_type` stays the fallback —
 * never invent a label the backend found no evidence for. The family label
 * ("KITAS / ITAS — Limited Stay Permit") only repeats the kind, so it is not
 * shown. */
function permitDisplayLabel(doc: ClientDocument): string {
  if (!doc.permit_label) {
    return isEpoDocument(doc)
      ? "EPO · Exit permit"
      : formatDocType(doc.document_type);
  }
  // A label that leads with its own visa index ("E23 — Working KITAS") reads
  // "E23 · Working KITAS" even when the backend sent no separate code.
  const code =
    doc.permit_code ||
    doc.permit_label.match(/^([A-Z][A-Z0-9]{1,7})\s*[—–-]\s/)?.[1];
  return (
    permitDisplay(code, doc.permit_label) || formatDocType(doc.document_type)
  );
}

/** An EPO (Exit Permit Only) document, matched case-insensitively on its
 * type, category, family and label. It belongs to the history, with its date,
 * never to a card grid of its own. */
function isEpoDocument(
  d: Pick<
    ClientDocument,
    "document_type" | "document_category" | "permit_family" | "permit_label"
  >,
): boolean {
  return /(^|[^a-z])epo([^a-z]|$)|exit[\s_-]*permit/i.test(
    `${d.document_type ?? ""} ${d.document_category ?? ""} ${d.permit_family ?? ""} ${d.permit_label ?? ""}`,
  );
}

/** An ITK is a stay permit (visit stay), a history kind like the KITAS family. */
const isItkDocument = (
  d: Pick<ClientDocument, "document_type" | "permit_family">,
): boolean =>
  /(^|[^a-z])itk([^a-z]|$)/i.test(
    `${d.document_type ?? ""} ${d.permit_family ?? ""}`,
  );

/** What a file IS, for the "Other files of this permit" list: the document
 * kind, not the permit name ("e-Visa", "KITAS card", "Sponsor letter"). */
function documentKindLabel(doc: ClientDocument): string {
  const t = (doc.document_type ?? "").toLowerCase();
  if (/e[-_ ]?visa/.test(t)) return "e-Visa";
  if (/e[-_ ]?voa/.test(t)) return "e-VOA";
  if (/kitas|itas/.test(t)) return "KITAS card";
  if (/kitap|itap/.test(t)) return "KITAP card";
  if (/\bitk\b/.test(t)) return "ITK";
  if (t.includes("visa")) return "Visa";
  const words = formatDocType(doc.document_type ?? "document").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// A MERP (Multiple Exit Re-entry Permit) document. `document_type` is often
// the literal string "MERP" and never matches `isVisaFamilyDocument` above
// (it is not itself a stay permit) — before this resolver existed it fell
// into the generic "Other" bucket instead of riding along with the current
// permit it re-enters on.
const isMerpDocument = (
  d: Pick<ClientDocument, "document_type" | "permit_family">,
) =>
  d.permit_family === "merp" ||
  (d.document_type?.toLowerCase().includes("merp") ?? false);

/** Shared download handler — same proxy-download logic renderDocCard used. */
/** The Drive id of a document's file: from the Drive URL, else the stored
 * file id, else a file URL. A document with none has nothing to download. */
function driveIdOf(doc: ClientDocument): string | null {
  return (
    (doc.google_drive_file_url &&
      extractDriveFileId(doc.google_drive_file_url)) ||
    doc.file_id ||
    (doc.file_url && extractDriveFileId(doc.file_url)) ||
    null
  );
}
const hasFile = (doc: ClientDocument): boolean => Boolean(driveIdOf(doc));

function downloadDocument(doc: ClientDocument) {
  const fileId = driveIdOf(doc);
  if (!fileId) return;
  const link = document.createElement("a");
  link.href = `/api/documents/proxy/${fileId}`;
  link.download = doc.file_name || `${doc.document_type}.pdf`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

// A document belongs to the "visa family" (current + previous-visa buckets):
// kitas, kitap, any visa (incl. e-visa), or a Visa on Arrival (incl. e-VOA).
// Single source of truth so the "current" and "history" matchers below
// cannot drift apart again — that drift is exactly what hid an unexpired
// VOA from "Actual Visa" while still surfacing it once expired.
export const isVisaFamilyDocument = (
  d: Pick<ClientDocument, "document_type">,
) => {
  const t = d.document_type?.toLowerCase() ?? "";
  return (
    t.includes("kitas") ||
    t.includes("kitap") ||
    t.includes("visa") || // also matches "e-visa" / "evisa"
    t.includes("voa") // also matches "e-voa" / "evoa"
  );
};

export function ImmigrationTab({
  clientId,
  documents,
  formatDate,
  onAddClick,
  onEditClick,
  onRefresh,
  onOpenFamilyTab,
  onOpenRemoved,
  day: dayProp,
}: {
  clientId: number;
  documents: ClientDocument[];
  formatDate: (d: string) => string;
  onAddClick: () => void;
  onEditClick: (doc: ClientDocument) => void;
  onRefresh: () => void;
  /** Switches the client card to its Family tab. */
  onOpenFamilyTab?: () => void;
  /** Opens the Documents "Removed or rejected" group. */
  onOpenRemoved?: () => void;
  /** The card's day (`YYYY-MM-DD`, the payload's `as_of`). */
  day?: string;
}) {
  const router = useRouter();
  const day = resolveCardDay(dayProp);

  const handleDelete = (docId: number, fileName: string) => {
    toast(`Archive document "${fileName || "Document"}"?`, {
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

  // Immigration docs: visas, permits, work authorizations — NO passports (shown in Overview)
  const isPassport = (d: ClientDocument) =>
    d.document_type?.toLowerCase().includes("passport");
  const isImmigrationDocument = (d: ClientDocument) =>
    !isPassport(d) &&
    (d.document_category === "immigration" ||
      isImmigrationDocType(d.document_type) ||
      isItkDocument(d) ||
      isEpoDocument(d));
  // Live only: a removed, archived or rejected document is neither current,
  // nor history, nor counted here. It is listed once, in Documents.
  const immigrationDocs = documents.filter(
    (d) => isImmigrationDocument(d) && isLive(d),
  );
  const voidOwnImmigration = documents.filter(
    (d) =>
      isImmigrationDocument(d) &&
      !isLive(d) &&
      !(d.family_member_id || d.family_member_name),
  ).length;

  // Sort: most recent expiry first, then by type
  const sortedAll = [...immigrationDocs].sort((a, b) => {
    if (a.expiry_date && b.expiry_date)
      return (
        new Date(b.expiry_date).getTime() - new Date(a.expiry_date).getTime()
      );
    if (a.expiry_date) return -1;
    return 1;
  });

  // A family member's permits are not the client's history (and carry no
  // countdown here): they live in the Family tab, signposted by one line.
  const isFamilyDoc = (d: ClientDocument) =>
    Boolean(d.family_member_id || d.family_member_name);
  const sortedDocs = sortedAll.filter((d) => !isFamilyDoc(d));
  const familyPermitCount = sortedAll.length - sortedDocs.length;

  // Current permit = the SAME pick the Overview card makes (pickCurrentVisa):
  // the latest by expiry among the client's own permits, ignoring removed and
  // archived ones. Only the current permit is alive, so only it gets a
  // countdown; everything older is history with its validity period.
  const actualVisa = pickCurrentVisa(sortedDocs);

  // Other files of the SAME permit as the current one (a re-upload, an
  // e-Visa of the same KITAS): shown under the current-permit panel, not as
  // history. Visa-family only; MERP and EPO have their own places.
  const sameFiles = actualVisa
    ? sortedDocs.filter(
        (d) =>
          (isVisaFamilyDocument(d) || isItkDocument(d)) &&
          !isMerpDocument(d) &&
          isSamePermit(d, actualVisa),
      )
    : [];

  // History = every other visa-family document (older or ended) plus the
  // EPO. Excludes anything MERP-classified: a document whose stored
  // document_type is generic ("visa") but whose resolved permit_family is
  // "merp" matches BOTH isVisaFamilyDocument (via the "visa" substring) and
  // isMerpDocument — without this exclusion it rendered once here AND once in
  // the MERP chip row below (GUILT fixed 2026-09-21).
  const previousVisas = sortedDocs.filter(
    (d) =>
      d !== actualVisa &&
      !sameFiles.includes(d) &&
      (isVisaFamilyDocument(d) || isItkDocument(d) || isEpoDocument(d)) &&
      !isMerpDocument(d),
  );

  // Working permits
  const workingPermits = sortedDocs.filter(
    (d) =>
      !isEpoDocument(d) &&
      !isItkDocument(d) &&
      (d.document_type?.toLowerCase().includes("permit") ||
        d.document_type?.toLowerCase().includes("imta") ||
        d.document_type?.toLowerCase().includes("rptka")),
  );

  // MERP (re-entry permit) — tied to the current permit, never "Other".
  // Only pulled out of "Other" when there IS a current permit to tie it to;
  // with no actualVisa it stays in otherDocs rather than disappearing.
  const merpDocs = actualVisa
    ? sortedDocs.filter((d) => d !== actualVisa && isMerpDocument(d))
    : [];

  // Other immigration docs (not in above categories)
  const otherDocs = sortedDocs.filter(
    (d) =>
      d !== actualVisa &&
      !sameFiles.includes(d) &&
      !previousVisas.includes(d) &&
      !workingPermits.includes(d) &&
      !merpDocs.includes(d),
  );

  // The date text of a non-current row. Neutral wording, never a countdown:
  // the period when both dates are known, "Ended"/"Valid until" for an expiry
  // alone, "Issued" for an issue date or (last resort) the upload date, so a
  // row never reads as two dashes while the record carries any date.
  const historyDateText = (doc: ClientDocument): string => {
    const { expiry, state } = documentValidity(doc, day);
    const issue = parseCalendarDate(doc.issue_date);
    if (issue && expiry) return `${formatDate(issue)} → ${formatDate(expiry)}`;
    if (expiry)
      return `${state === "expired" ? "Ended " : "Valid until "}${formatDate(expiry)}`;
    if (issue) return `Issued ${formatDate(issue)}`;
    if (doc.created_at)
      return `Issued ${formatDate(doc.created_at.slice(0, 10))}`;
    return "—";
  };

  // Row actions shared by the history, the compact lists and the panel. The
  // file name is only ever the download button's title/aria-label.
  const rowActions = (doc: ClientDocument, withRenew = true) => (
    <div className="flex items-center justify-end gap-0.5 px-2.5">
      {withRenew && isRenewable(doc, day) && (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={() =>
            router.push(`/process/new?client_id=${clientId}&type=visa_renewal`)
          }
          aria-label={`Start renewal for ${doc.document_type}`}
          title="Start Renewal"
        >
          <RefreshCw className="w-3 h-3" />
        </Button>
      )}
      {hasFile(doc) && (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={() => downloadDocument(doc)}
          aria-label={`Download ${doc.file_name || doc.document_type}`}
          title={doc.file_name || `Download ${doc.document_type}`}
        >
          <Download className="w-3 h-3" />
        </Button>
      )}
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        onClick={() => onEditClick(doc)}
        aria-label={`Edit ${doc.document_type}`}
      >
        <Edit2 className="w-3 h-3" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        onClick={() => handleDelete(doc.id, doc.file_name || doc.document_type)}
        aria-label={`Remove ${doc.document_type}`}
      >
        <Trash2 className="w-3 h-3" />
      </Button>
    </div>
  );

  // A compact list (no image preview): label, validity, actions. Neutral:
  // a countdown or a warning tone lives only on the current-permit panel.
  // `byKind` labels a row by what the file is (e-Visa, KITAS card ...).
  const compactRows = (id: string, docs: ClientDocument[], byKind = false) => (
    <HairlineGrid
      cols="minmax(0,1.6fr) minmax(0,1.2fr) 140px"
      id={id}
      className="max-sm:[&_.grid]:!grid-cols-[minmax(0,1fr)_auto]"
    >
      <HairlineHead>
        <span>Document</span>
        <span className="max-sm:hidden">Validity</span>
        <span className="sr-only">Actions</span>
      </HairlineHead>
      <HairlineBody>
        {docs.map((doc) => {
          const validity = historyDateText(doc);
          return (
            <HairlineRow key={doc.id}>
              <div className="min-w-0 px-2.5">
                <CellStack
                  primary={
                    byKind ? documentKindLabel(doc) : permitDisplayLabel(doc)
                  }
                />
                <span className="mt-1 block text-[11px] text-[var(--tx-secondary)] sm:hidden">
                  {validity}
                </span>
              </div>
              <span className="max-sm:hidden whitespace-nowrap">
                {validity}
              </span>
              {rowActions(doc, false)}
            </HairlineRow>
          );
        })}
      </HairlineBody>
    </HairlineGrid>
  );

  // A small card, no image: title, key dates, and Download / Upload at the
  // bottom-left like the Passport and Visa cards (edit and remove stay right).
  const docCard = (doc: ClientDocument) => (
    <div
      key={doc.id}
      className="flex flex-col gap-3 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-card)] p-4"
    >
      <div className="min-w-0">
        <p className="truncate text-[15px] font-medium text-[var(--tx-pure)]">
          {permitDisplayLabel(doc)}
        </p>
        <p className="mt-1 text-[12.5px] text-[var(--tx-secondary)]">
          {historyDateText(doc)}
        </p>
      </div>
      <div className="mt-auto flex flex-wrap items-center gap-2">
        {hasFile(doc) && (
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5 shadow-none"
            onClick={() => downloadDocument(doc)}
            aria-label={`Download ${doc.file_name || doc.document_type}`}
            title={doc.file_name || `Download ${doc.document_type}`}
          >
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
            Download
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          className="gap-1.5 shadow-none"
          onClick={onAddClick}
          aria-label={`Upload a file for ${doc.document_type}`}
        >
          <Upload className="h-3.5 w-3.5" aria-hidden="true" />
          Upload
        </Button>
        <span className="ml-auto flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => onEditClick(doc)}
            aria-label={`Edit ${doc.document_type}`}
          >
            <Edit2 className="w-3 h-3" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() =>
              handleDelete(doc.id, doc.file_name || doc.document_type)
            }
            aria-label={`Remove ${doc.document_type}`}
          >
            <Trash2 className="w-3 h-3" />
          </Button>
        </span>
      </div>
    </div>
  );

  const sections = [
    {
      title: "Working Permit",
      docs: workingPermits,
      id: "immigration-working",
    },
    { title: "Other", docs: otherDocs, id: "immigration-other" },
  ];

  // The Issued column exists only when some history row has an issue date.
  // The Issued value: the issue date wherever the payload carries it (no
  // guessed fallback).
  const issuedOf = (d: ClientDocument): string | undefined =>
    d.issue_date || undefined;
  const showIssued = [
    ...(actualVisa ? [actualVisa] : []),
    ...previousVisas,
  ].some((d) => issuedOf(d));
  // The history shows the current permit first, marked and larger.
  const historyRows = [...(actualVisa ? [actualVisa] : []), ...previousVisas];

  const currentValidity = actualVisa ? documentValidity(actualVisa, day) : null;
  const expiryTone = actualVisa
    ? isUrgent(actualVisa, day)
      ? "text-[var(--state-warning)]"
      : "text-[var(--tx-pure)]"
    : "text-[var(--tx-secondary)]";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-[var(--bz-text-1)]">
          Immigration
        </h3>
        <Button
          size="sm"
          variant="outline"
          className="gap-2 border-[var(--state-success)] bg-[var(--state-success)] text-white hover:bg-[var(--state-success)] hover:opacity-90"
          onClick={onAddClick}
        >
          <Plus className="w-4 h-4" />
          Add Document
        </Button>
      </div>

      {actualVisa && (
        <div className="rounded-lg border border-[var(--bz-border)] bg-[var(--bz-card)] p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="mb-2 text-[10px] font-[650] uppercase tracking-[0.14em] text-[var(--tx-secondary)]">
                Current permit
              </p>
              <span
                className={cn(
                  "inline-flex items-center rounded-[6px] bg-[var(--bz-kita-ink-fill)] px-3 py-1.5 text-[19px] font-medium text-[var(--bz-base)]",
                  // Capitalize only the raw
                  // document_type fallback, never an official catalogue name.
                  !actualVisa.permit_label && "capitalize",
                )}
                style={{ fontFamily: "var(--font-serif)" }}
              >
                {permitDisplayLabel(actualVisa)}
              </span>
            </div>
            <AiRecap
              clientId={clientId}
              scope="immigration"
              className="order-last w-full sm:order-none sm:w-auto sm:min-w-[240px] sm:max-w-[52ch] sm:flex-1"
            />
            <div className="flex items-center gap-1">
              {hasFile(actualVisa) && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => downloadDocument(actualVisa)}
                  aria-label="Download current permit"
                  title={actualVisa.file_name || "Download current permit"}
                >
                  <Download className="w-3 h-3" />
                </Button>
              )}
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => onEditClick(actualVisa)}
                aria-label="Edit current permit"
              >
                <Edit2 className="w-3 h-3" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
                onClick={() =>
                  handleDelete(
                    actualVisa.id,
                    actualVisa.file_name || actualVisa.document_type,
                  )
                }
                aria-label="Remove current permit"
                title="Remove current permit"
              >
                <Trash2 className="w-3 h-3" />
              </Button>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-end gap-7">
            {parseCalendarDate(actualVisa.issue_date) && (
              <div>
                <Numeral
                  n={Math.max(
                    0,
                    dayNumber(day) -
                      dayNumber(parseCalendarDate(actualVisa.issue_date)!),
                  )}
                  size="kpi"
                  tone="wait"
                  className="text-[var(--tx-pure)]"
                />
                <p className="mt-1.5 text-[11px] font-[650] uppercase tracking-[0.06em] text-[var(--tx-secondary)]">
                  Days on this permit
                </p>
              </div>
            )}
            {currentValidity && currentValidity.daysLeft !== null && (
              <div>
                <Numeral
                  n={
                    currentValidity.state === "expired"
                      ? -currentValidity.daysLeft
                      : currentValidity.daysLeft
                  }
                  size="kpi"
                  tone="wait"
                  className={expiryTone}
                />
                <p className="mt-1.5 text-[11px] font-[650] uppercase tracking-[0.06em] text-[var(--tx-secondary)]">
                  {currentValidity.state === "expired"
                    ? "Days past expiry"
                    : "Days to expiry"}
                </p>
              </div>
            )}
          </div>

          <ValidityTrack
            issueDate={actualVisa.issue_date}
            expiryDate={actualVisa.expiry_date}
            day={day}
          />

          <div className="mt-1 flex flex-wrap gap-5 text-[12.5px] text-[var(--tx-secondary)]">
            {parseCalendarDate(actualVisa.issue_date) && (
              <span>
                Issued{" "}
                <b className="font-semibold text-[var(--tx-pure)]">
                  {formatDate(parseCalendarDate(actualVisa.issue_date)!)}
                </b>
              </span>
            )}
            {currentValidity?.expiry && currentValidity.daysLeft !== null && (
              <span>
                Expires{" "}
                <b className={cn("font-semibold", expiryTone)}>
                  {formatDate(currentValidity.expiry)} ·{" "}
                  {expiryWording(currentValidity.daysLeft)}
                </b>
              </span>
            )}
            {actualVisa.permit_number && (
              <span>
                Permit no.{" "}
                <b className="font-semibold text-[var(--tx-pure)]">
                  {actualVisa.permit_number}
                </b>
              </span>
            )}
            {actualVisa.permit_sponsor && (
              <span>
                Sponsor{" "}
                <b className="font-semibold text-[var(--tx-pure)]">
                  {actualVisa.permit_sponsor}
                </b>
              </span>
            )}
            {actualVisa.family_member_name && (
              <span>
                For{" "}
                <b className="font-semibold text-[var(--tx-pure)]">
                  {actualVisa.family_member_name}
                </b>
              </span>
            )}
          </div>

          {sameFiles.length > 0 && (
            <div className="mt-4">
              <p className="mb-1 text-[12.5px] font-medium text-[var(--tx-secondary)]">
                Other files of this permit
              </p>
              {compactRows("immigration-same-permit", sameFiles, true)}
            </div>
          )}

          {merpDocs.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {merpDocs.map((doc) => (
                <span
                  key={doc.id}
                  className="inline-flex items-center gap-1.5 rounded-[6px] border border-[var(--bz-border)] bg-[var(--bz-base)] px-2.5 py-1 text-[12px] text-[var(--tx-secondary)]"
                >
                  <RefreshCw className="w-3 h-3" />
                  {permitDisplayLabel(doc)}
                  {documentValidity(doc, day).expiry &&
                    ` · Exp ${formatDate(documentValidity(doc, day).expiry!)}`}
                </span>
              ))}
            </div>
          )}

          {isRenewable(actualVisa, day) && (
            <Button
              variant="outline"
              size="sm"
              className="mt-3 gap-1.5 shadow-none"
              onClick={(e) => {
                e.stopPropagation();
                router.push(
                  `/process/new?client_id=${clientId}&type=visa_renewal`,
                );
              }}
            >
              <RefreshCw className="w-3 h-3" />
              Start Renewal
            </Button>
          )}
        </div>
      )}

      {previousVisas.length > 0 && (
        <LedgerSection
          n={1}
          title="Visa history"
          actions={
            <span className="text-[13px] text-[var(--tx-secondary)]">
              ({previousVisas.length})
            </span>
          }
        >
          <HairlineGrid
            cols={
              showIssued
                ? "1.5fr 0.7fr 0.9fr 1.5fr 140px"
                : "1.6fr 0.8fr 1.5fr 140px"
            }
            colsCollapsed="1.6fr 1fr 1fr 140px"
            id="immigration-visa-history"
            // `colsCollapsed`'s own scoped style sets `--cols` under a media
            // query, but an inline `--cols` on the very same element already
            // wins over it (PR 6520, not this window's to fix — see
            // DocumentsTab.tsx:41-49, FamilyTab.tsx:444 and
            // ObligationsTable.tsx ~475-493, which document and route around
            // the same defect). Its `[data-collapse]{display:none}` half
            // does apply, so the "Issued" cells still leave the flow; only
            // the track count needed the same technique those three
            // siblings use: override `grid-template-columns` directly on
            // the `.grid` descendant, scoped and `!important`, at the same
            // 1360px breakpoint `colsCollapsed` was already targeting.
            //
            // Below 640px (`max-sm`) Status and Expires leave the grid the
            // same way, on top of the 1360px step above — their content
            // relocates onto the first cell's own secondary block instead,
            // the same technique DocumentsTab.tsx uses (PR 6889), narrowing
            // the row to Visa type + Actions on a phone width.
            className="max-[1360px]:[&_.grid]:!grid-cols-[1.6fr_1fr_1fr_140px] max-sm:[&_.grid]:!grid-cols-[minmax(0,1fr)_auto]"
          >
            <HairlineHead>
              <span>Visa type</span>
              <span className="max-sm:hidden">Status</span>
              {showIssued && <span data-collapse>Issued</span>}
              <span className="max-sm:hidden">Validity</span>
              <span className="sr-only">Actions</span>
            </HairlineHead>
            <HairlineBody>
              {historyRows.map((doc) => {
                // Every history row is neutral: no countdown, no warning tone.
                // The current permit is marked and larger; the rest are smaller.
                const current = doc === actualVisa;
                const statusNode = (
                  <span
                    className={cn(
                      "inline-flex items-center rounded-[2px] px-2 py-0.5 text-[11px] font-medium",
                      current ? CURRENT_PILL : COMPLETED_PILL,
                    )}
                  >
                    {current
                      ? doc.status === "verified"
                        ? "Verified"
                        : "Current"
                      : "Completed"}
                  </span>
                );
                const expiresText = historyDateText(doc);
                const issued = issuedOf(doc);
                return (
                  <HairlineRow
                    key={doc.id}
                    data-current={current ? "true" : undefined}
                  >
                    <div className="min-w-0 px-2.5">
                      <CellStack
                        primary={
                          <span
                            className={cn(
                              current
                                ? "border-l-2 border-[var(--tx-pure)] pl-2 text-[17px] font-semibold text-[var(--tx-pure)]"
                                : "text-[13px] text-[var(--tx-secondary)]",
                            )}
                          >
                            {permitDisplayLabel(doc)}
                          </span>
                        }
                        collapsed={
                          issued ? `Issued ${formatDate(issued)}` : undefined
                        }
                      />
                      <span className="mt-1 flex flex-col gap-0.5 text-[11px] text-[var(--tx-secondary)] sm:hidden">
                        {statusNode}
                        <span>{expiresText}</span>
                      </span>
                    </div>
                    <span className="max-sm:hidden">{statusNode}</span>
                    {showIssued && (
                      <span
                        data-collapse
                        className={cn(!current && "text-[12.5px]")}
                      >
                        {issued ? formatDate(issued) : "—"}
                      </span>
                    )}
                    <span
                      className={cn(
                        "max-sm:hidden whitespace-nowrap",
                        !current && "text-[12.5px] text-[var(--tx-secondary)]",
                      )}
                    >
                      {expiresText}
                    </span>
                    {rowActions(doc)}
                  </HairlineRow>
                );
              })}
            </HairlineBody>
          </HairlineGrid>
        </LedgerSection>
      )}

      {familyPermitCount > 0 &&
        (onOpenFamilyTab ? (
          <p className="text-[12.5px] text-[var(--tx-secondary)]">
            <button
              type="button"
              onClick={onOpenFamilyTab}
              className={cn(
                "underline underline-offset-4 hover:text-[var(--tx-pure)]",
                FOCUS,
              )}
            >
              Family permits are in the Family tab ({familyPermitCount})
            </button>
          </p>
        ) : (
          <p className="text-[12.5px] text-[var(--tx-secondary)]">
            Family permits are in the Family tab ({familyPermitCount})
          </p>
        ))}

      <RemovedSignpost count={voidOwnImmigration} onOpen={onOpenRemoved} />

      {immigrationDocs.length === 0 ? (
        <div className="rounded-xl border border-dashed border-[var(--bz-border)] bg-[var(--bz-card)] p-12 text-center shadow-[var(--bz-shadow-card)]">
          <Globe className="w-12 h-12 mx-auto text-[var(--bz-text-2)] mb-3 opacity-50" />
          <p className="text-[var(--bz-text-2)]">
            No immigration documents yet
          </p>
          <p className="text-sm text-[var(--bz-text-2)] mt-1">
            Upload KITAS, visa, or working permit documents
          </p>
        </div>
      ) : (
        sections.map(({ title, docs: sectionDocs, id }) => {
          if (sectionDocs.length === 0) return null;
          return (
            <div key={title} className="space-y-2">
              <h4 className="flex items-baseline gap-2 text-[15px] font-medium text-[var(--tx-pure)]">
                {title}
                <span className="text-[13px] font-normal text-[var(--tx-secondary)]">
                  ({sectionDocs.length})
                </span>
              </h4>
              <div
                data-section={id}
                className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3"
              >
                {sectionDocs.map(docCard)}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}

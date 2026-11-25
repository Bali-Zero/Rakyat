"use client";

import React, { useRef, useState } from "react";
import {
  Download,
  Edit2,
  Eye,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  CellStack,
  EmptyState,
  HairlineBody,
  HairlineGrid,
  HairlineHead,
  HairlineRow,
  LedgerSection,
  Numeral,
  StatePill,
} from "@/components/workspace/r19";
import { useOcrPolling } from "@/hooks/useOcrPolling";
import { api } from "@/lib/api";
import type { ClientDocument, FamilyMember } from "@/lib/api/crm/crm.types";
import { fileToBase64 } from "@/lib/utils";
import {
  BASE64_UPLOAD_MAX_BYTES,
  BASE64_UPLOAD_MAX_LABEL,
} from "@/lib/upload/base64UploadLimit";
import { extractDriveFileId } from "./utils";
import { expiryWording, isLive } from "./documentValidity";
import {
  familyPassport,
  familyPermit,
  levelPill,
  pickMemberPassport,
} from "./familyValidity";
import { documentPermitLabel } from "./permit-label";
import { pickCurrentPermitFor } from "./pickCurrentVisa";
import { resolveCardDay } from "./witaClock";
import { RemovedSignpost } from "./RemovedSignpost";

type DocumentKind = "passport" | "visa";

function FamilyMemberUploadButton({
  clientId,
  memberId,
  memberName,
  documentType,
  onRefresh,
  replace,
}: {
  clientId: number;
  memberId: number;
  memberName: string;
  documentType: DocumentKind;
  onRefresh: () => Promise<void> | void;
  /**
   * The "Replace stay permit" control: the same upload (a new permit file
   * for the member, `document_type: "visa"`), with a visible label. Nothing
   * is deleted or archived: the newer upload becomes the current permit
   * (`pickCurrentPermitFor`) and the old one stays on file as history.
   * `fieldTyped` = the member's own visa fields set the shown permit, so
   * they keep winning until staff edit them.
   */
  replace?: { fieldTyped: boolean };
}) {
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { ocrPolling, pollOcrStatus } = useOcrPolling({
    clientId,
    onDone: onRefresh,
  });
  const label = `${documentType === "passport" ? "Passport" : "Visa"}`;
  // The old button's visible word changed with the request state (OLD:126-129
  // — "Uploading..." / "OCR in corso...") and the icon-only rebuild dropped
  // it silently, leaving a spinner with a static accessible name — invisible
  // on touch, per IMPLEMENTER-RULES.md ("tooltip-only information is
  // invisible on touch: put it in a readable sub-line"). `statusLabel` still
  // carries the full context on the accessible name; `statusWord` is the
  // short word rendered next to the spinner ONLY while a state is active, so
  // the idle control stays icon-only.
  const what = replace ? "stay permit" : documentType;
  const statusLabel = isUploading
    ? `Uploading ${what} for ${memberName}`
    : ocrPolling
      ? `OCR in corso for ${memberName}`
      : replace
        ? `Replace stay permit for ${memberName}`
        : `Upload ${documentType} for ${memberName}`;
  const statusWord = isUploading
    ? "Uploading..."
    : ocrPolling
      ? "OCR in corso..."
      : null;

  const handleUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const allowedTypes = [
      "image/jpeg",
      "image/jpg",
      "image/png",
      "application/pdf",
    ];
    if (!allowedTypes.includes(file.type)) {
      toast.error("Invalid file type", {
        description: "Please upload JPG, PNG, or PDF",
      });
      return;
    }
    if (file.size > BASE64_UPLOAD_MAX_BYTES) {
      toast.error("File too large", {
        description: `Maximum ${BASE64_UPLOAD_MAX_LABEL}`,
      });
      return;
    }

    setIsUploading(true);
    try {
      const response = (await api.post(
        `/api/crm/clients/${clientId}/documents/upload`,
        {
          file: await fileToBase64(file),
          file_name: file.name,
          document_type: documentType,
          mime_type: file.type,
          family_member_id: memberId,
        },
      )) as { success: boolean; message?: string };
      if (response.success) {
        if (replace) {
          toast.success(
            `New stay permit uploaded for ${memberName} — OCR in corso...`,
            {
              description: replace.fieldTyped
                ? "The previous permit stays on file. The visa type and expiry typed on this member still show until you edit the member."
                : "The previous permit stays on file as history.",
            },
          );
        } else {
          toast.success(
            `${label} uploaded for ${memberName} — OCR in corso...`,
          );
        }
        pollOcrStatus();
      } else {
        toast.error("Upload failed", { description: response.message });
      }
    } catch (error) {
      toast.error("Upload failed", { description: (error as Error).message });
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  return (
    <>
      <input
        ref={fileInputRef}
        type="file"
        accept=".jpg,.jpeg,.png,.pdf"
        className="hidden"
        onChange={handleUpload}
        disabled={isUploading || ocrPolling}
      />
      <Button
        type="button"
        variant="ghost"
        size={replace ? "sm" : "icon"}
        className={
          replace
            ? "h-8 gap-1.5 px-2 text-xs text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
            : "h-8 w-8 text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
        }
        aria-label={statusLabel}
        onClick={() => fileInputRef.current?.click()}
        disabled={isUploading || ocrPolling}
      >
        {isUploading || ocrPolling ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : replace ? (
          <RefreshCw className="h-3.5 w-3.5" />
        ) : (
          <Upload className="h-4 w-4" />
        )}
        {replace && !statusWord ? "Replace stay permit" : null}
      </Button>
      {statusWord ? (
        <span className="text-[11px] text-[var(--tx-secondary)]">
          {statusWord}
        </span>
      ) : null}
    </>
  );
}

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

function ageAtPresent(dateOfBirth: string) {
  return Math.floor(
    (Date.now() - new Date(dateOfBirth).getTime()) / (365.25 * 86400000),
  );
}

/**
 * The visa pill when no date can be read (no field, no live dated document):
 * the member either has a permit type recorded or has none. The enum is not
 * read here — a family enum is read only with a date under it.
 */
function undatedVisaPill(hasType: boolean) {
  return {
    tone: "wait" as const,
    label: hasType ? "Visa on file" : "No visa",
  };
}

/** A date cell's second line: the shared wording ladder, urgent or quiet. */
function expiryLine(
  result: ReturnType<typeof familyPermit>,
): { label: string; urgent: boolean } | undefined {
  if (!result.validity || result.validity.daysLeft === null) return undefined;
  return {
    label: expiryWording(result.validity.daysLeft),
    urgent: result.urgent,
  };
}

function DocumentActionButton({
  action,
  documentType,
  memberName,
  url,
}: {
  action: "view" | "download";
  documentType: DocumentKind;
  memberName: string;
  url: string;
}) {
  const label = `${action === "view" ? "View" : "Download"} ${documentType} for ${memberName}`;
  const Icon = action === "view" ? Eye : Download;

  const onClick = () => {
    const fileId = extractDriveFileId(url);
    if (!fileId) return;
    const proxyUrl = `/api/documents/proxy/${fileId}`;
    if (action === "view") {
      window.open(proxyUrl, "_blank", "noopener,noreferrer");
      return;
    }
    const link = document.createElement("a");
    link.href = proxyUrl;
    link.download = `${documentType}_${memberName.replace(/\s+/g, "_")}.jpg`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="h-8 w-8 text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      <Icon className="h-4 w-4" />
    </Button>
  );
}

function MemberActions({
  clientId,
  member,
  passportDocument,
  visaDocument,
  onEditClick,
  onRefresh,
  onDelete,
}: {
  clientId: number;
  member: FamilyMember;
  passportDocument?: ClientDocument;
  visaDocument?: ClientDocument;
  onEditClick: (member: FamilyMember) => void;
  onRefresh: () => Promise<void> | void;
  onDelete: (id: number, name: string) => void;
}) {
  const documentControls = (
    documentType: DocumentKind,
    document?: ClientDocument,
  ) => (
    <div
      key={documentType}
      role="group"
      aria-label={documentType === "passport" ? "Passport" : "Visa"}
      className="ml-2 flex items-center gap-0.5"
    >
      <span className="pr-1 text-[11px] text-[var(--tx-secondary)]">
        {documentType === "passport" ? "Passport" : "Visa"}
      </span>
      {document?.google_drive_file_url ? (
        <>
          <DocumentActionButton
            action="view"
            documentType={documentType}
            memberName={member.full_name}
            url={document.google_drive_file_url}
          />
          <DocumentActionButton
            action="download"
            documentType={documentType}
            memberName={member.full_name}
            url={document.google_drive_file_url}
          />
        </>
      ) : null}
      <FamilyMemberUploadButton
        clientId={clientId}
        memberId={member.id}
        memberName={member.full_name}
        documentType={documentType}
        onRefresh={onRefresh}
      />
    </div>
  );

  return (
    <div className="flex min-h-11 items-center justify-end gap-0.5 px-2.5 max-[640px]:justify-start">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-8 w-8 text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
        aria-label={`Edit ${member.full_name}`}
        title={`Edit ${member.full_name}`}
        onClick={() => onEditClick(member)}
      >
        <Edit2 className="h-4 w-4" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-8 w-8 text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
        aria-label={`Remove ${member.full_name}`}
        title={`Remove ${member.full_name}`}
        onClick={() => onDelete(member.id, member.full_name)}
      >
        <Trash2 className="h-4 w-4" />
      </Button>
      {documentControls("passport", passportDocument)}
      {documentControls("visa", visaDocument)}
    </div>
  );
}

export function FamilyTab({
  clientId,
  familyMembers,
  documents,
  formatDate,
  onAddClick,
  onEditClick,
  onRefresh,
  onOpenRemoved,
  day: dayProp,
}: {
  clientId: number;
  familyMembers: FamilyMember[];
  documents: ClientDocument[];
  formatDate: (date: string) => string;
  onAddClick: () => void;
  onEditClick: (member: FamilyMember) => void;
  onRefresh: () => Promise<void> | void;
  /** Opens the Documents "Removed or rejected" group. */
  onOpenRemoved?: () => void;
  /** The card's day (`YYYY-MM-DD`, the payload's `as_of`). */
  day?: string;
}) {
  const day = resolveCardDay(dayProp);
  // Void documents of family members are not shown on the members: one line
  // points at the Documents group that lists them.
  const voidFamilyDocuments = documents.filter(
    (d) => Boolean(d.family_member_id || d.family_member_name) && !isLive(d),
  ).length;
  const handleDelete = (id: number, name: string) => {
    toast(`Remove ${name} from family members?`, {
      action: {
        label: "Remove",
        onClick: async () => {
          try {
            await api.crm.deleteFamilyMember(clientId, id);
            toast.success("Family member removed");
            await onRefresh();
          } catch (error) {
            toast.error("Error", { description: (error as Error).message });
          }
        },
      },
      cancel: { label: "Cancel", onClick: () => toast.dismiss() },
    });
  };

  const addAction = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="gap-2 border-[var(--line-control)] text-[var(--tx-pure)] hover:bg-[var(--bz-card)]"
      aria-label="Add family member"
      onClick={onAddClick}
    >
      <Plus className="h-4 w-4" />
      Add family member
    </Button>
  );

  return (
    <LedgerSection
      n={6}
      title={
        <span className="flex items-baseline gap-2">
          Family <Numeral n={familyMembers.length} size="count" />
        </span>
      }
      actions={addAction}
    >
      {familyMembers.length === 0 ? (
        <EmptyState>No family members on file.</EmptyState>
      ) : (
        <HairlineGrid
          cols="minmax(16rem, 1.8fr) minmax(9rem, 1fr) minmax(10rem, 0.9fr) minmax(18rem, auto)"
          // `colsCollapsed` + `collapseAt` + `id` is inert on this primitive
          // (#6520, see ObligationsTable ~471-487): the collapse stylesheet
          // sets `--cols` under a media query, and an inline style already
          // sets `--cols` on the same element, so the rule never wins
          // without `!important`. This overrides the template on the rows
          // themselves instead, which does win.
          className="max-[640px]:[&_.grid]:!grid-cols-[minmax(0,1fr)]"
        >
          <HairlineHead className="max-[640px]:hidden">
            <span>Member</span>
            <span>Visa</span>
            <span>Expires</span>
            <span>Actions</span>
          </HairlineHead>
          <HairlineBody>
            {familyMembers.map((member) => {
              // The SAME documents `familyPermit` / `familyPassport` date
              // from, so the type, the date and the file icons describe one
              // permit (the main client's `pickCurrentVisa` rule).
              const passportDocument = pickMemberPassport(documents, member.id);
              const visaDocument = pickCurrentPermitFor(
                [...documents],
                member.id,
              );
              const permit = familyPermit(member, documents, day);
              const passport = familyPassport(member, documents, day);
              // The type follows the date's source: the member's typed field
              // when the date is typed (or there is none), else the permit
              // the server resolved on the document (as the Immigration
              // panel shows it). Never the raw `document_type`: a document on
              // file with nothing extracted yet is not a visa TYPE, it is
              // OCR pending — see the note rendered below.
              const documentPermit = visaDocument
                ? documentPermitLabel(visaDocument)
                : "";
              const visaType =
                (permit.source === "document"
                  ? documentPermit || member.current_visa_type
                  : member.current_visa_type || documentPermit) || undefined;
              const pill = permit.level
                ? levelPill(permit.level)
                : undatedVisaPill(Boolean(visaType || visaDocument));
              const hasPermit = Boolean(
                visaDocument || member.current_visa_type || permit.expiry,
              );
              const visaUrgencyInfo = expiryLine(permit);
              const passportUrgencyInfo = expiryLine(passport);
              const personalDetails = [
                member.date_of_birth
                  ? `Born ${formatDate(member.date_of_birth)} · ${ageAtPresent(member.date_of_birth)}y`
                  : undefined,
                member.email,
                member.phone,
                member.passport_number
                  ? `Passport ${member.passport_number}`
                  : undefined,
              ].filter(Boolean);

              return (
                <HairlineRow
                  key={member.id}
                  data-testid={`family-member-row-${member.id}`}
                >
                  <div className="min-w-0 px-2.5 py-3">
                    <CellStack
                      primary={
                        <span className="flex min-w-0 items-center gap-3">
                          <span
                            aria-hidden="true"
                            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[6px] bg-[var(--bz-card)] text-sm text-[var(--tx-pure)]"
                          >
                            {initials(member.full_name)}
                          </span>
                          <span className="truncate">{member.full_name}</span>
                        </span>
                      }
                      secondary={[member.relationship, member.nationality]
                        .filter(Boolean)
                        .join(" · ")}
                    />
                    {personalDetails.length > 0 || member.notes ? (
                      <div className="mt-2 space-y-1 text-xs text-[var(--tx-secondary)]">
                        {personalDetails.length > 0 ? (
                          <p>{personalDetails.join(" · ")}</p>
                        ) : null}
                        {member.notes ? <p>Notes: {member.notes}</p> : null}
                      </div>
                    ) : null}
                  </div>
                  <div className="px-2.5 py-3">
                    <CellStack
                      primary={
                        visaType ?? (visaDocument ? "—" : "No visa on file")
                      }
                      secondary={
                        <StatePill tone={pill.tone} label={pill.label} />
                      }
                    />
                    {visaDocument && !visaType ? (
                      <p className="mt-1 text-xs text-[var(--tx-secondary)]">
                        Document on file — upload to extract data via OCR
                      </p>
                    ) : null}
                    {hasPermit ? (
                      <div className="-ml-2 mt-1 flex items-center gap-1">
                        <FamilyMemberUploadButton
                          clientId={clientId}
                          memberId={member.id}
                          memberName={member.full_name}
                          documentType="visa"
                          onRefresh={onRefresh}
                          replace={{ fieldTyped: permit.source === "field" }}
                        />
                      </div>
                    ) : null}
                  </div>
                  <div className="space-y-1 px-2.5 py-3 text-[13px] text-[var(--tx-pure)]">
                    {permit.expiry ? (
                      <p>{formatDate(permit.expiry)}</p>
                    ) : (
                      <p>—</p>
                    )}
                    {visaUrgencyInfo ? (
                      <p
                        className="text-xs"
                        style={{
                          color: visaUrgencyInfo.urgent
                            ? "var(--state-warning)"
                            : "var(--tx-secondary)",
                        }}
                      >
                        {visaUrgencyInfo.label}
                      </p>
                    ) : null}
                    {passport.expiry ? (
                      <p className="pt-1 text-xs text-[var(--tx-secondary)]">
                        Passport expires{" "}
                        <span className="text-[var(--tx-pure)]">
                          {formatDate(passport.expiry)}
                        </span>
                      </p>
                    ) : null}
                    {passportUrgencyInfo ? (
                      <p
                        className="text-xs"
                        style={{
                          color: passportUrgencyInfo.urgent
                            ? "var(--state-warning)"
                            : "var(--tx-secondary)",
                        }}
                      >
                        Passport: {passportUrgencyInfo.label}
                      </p>
                    ) : null}
                    {passportDocument && !member.passport_number ? (
                      <p className="text-xs text-[var(--tx-secondary)]">
                        Document on file — upload to extract data via OCR
                      </p>
                    ) : null}
                  </div>
                  <MemberActions
                    clientId={clientId}
                    member={member}
                    passportDocument={passportDocument}
                    visaDocument={visaDocument}
                    onEditClick={onEditClick}
                    onRefresh={onRefresh}
                    onDelete={handleDelete}
                  />
                </HairlineRow>
              );
            })}
          </HairlineBody>
        </HairlineGrid>
      )}
      <RemovedSignpost
        count={voidFamilyDocuments}
        onOpen={onOpenRemoved}
        className="pt-3"
      />
    </LedgerSection>
  );
}

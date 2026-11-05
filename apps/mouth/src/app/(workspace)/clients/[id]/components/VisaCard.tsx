"use client";

import React, { useState, useRef } from "react";
import { FileText, Loader2, Upload, Download, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { logger } from "@/lib/logger";
import { api } from "@/lib/api";
import { fileToBase64 } from "@/lib/utils";
import {
  BASE64_UPLOAD_MAX_BYTES,
  BASE64_UPLOAD_MAX_LABEL,
} from "@/lib/upload/base64UploadLimit";
import { useOcrPolling } from "@/hooks/useOcrPolling";
import type { ClientProfile, ClientDocument } from "@/lib/api/crm/crm.types";
import { extractDriveFileId, safeExternalUrl } from "./utils";
import { pickCurrentVisa } from "./pickCurrentVisa";
import { daysUntil, elapsedShare, humanize } from "./overview-brief";
import { permitLine } from "./header-facts";
import {
  CardFrame,
  expiryTone,
  ExpiryText,
  Fact,
  Facts,
  NOT_RECORDED,
  Runway,
} from "./cardKit";

export function VisaCard({
  client,
  documents,
  activePractices,
  formatDate,
  formatCurrency,
  onRefresh,
  clientId,
}: {
  client: ClientProfile["client"];
  documents: ClientDocument[];
  activePractices: ClientProfile["practices"];
  formatDate: (d: string) => string;
  formatCurrency: (n: number) => string;
  onRefresh: () => Promise<void>;
  clientId: number;
}) {
  const [isUploading, setIsUploading] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isExtracting, setIsExtracting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Poll OCR status after upload
  // NOTE: cleanupOnUnmount=false reproduces this copy's original gap — it
  // never guarded against firing setState after unmount. Not fixed here
  // per the "no behaviour change" mandate; see PR body.
  const { ocrPolling, pollOcrStatus } = useOcrPolling({
    clientId,
    onDone: onRefresh,
    cleanupOnUnmount: false,
  });

  // The current visa/KITAS — see pickCurrentVisa.
  const latestVisa = pickCurrentVisa(documents);
  const latestVisaFileUrl = latestVisa?.google_drive_file_url;

  // Find active visa process
  const visaProcess = activePractices.find(
    (p) =>
      p.practice_type_code?.toLowerCase().includes("visa") ||
      p.practice_type_code?.toLowerCase().includes("kitas") ||
      p.practice_type_code?.toLowerCase().includes("kitap") ||
      p.practice_type_name?.toLowerCase().includes("visa") ||
      p.practice_type_name?.toLowerCase().includes("kitas"),
  );

  // Get visa dates from document metadata or practice
  const visaStartDate = latestVisa?.issue_date || visaProcess?.start_date;
  const visaExpiryDate = latestVisa?.expiry_date;
  const nowMs = Date.now();
  const expiryDays = daysUntil(visaExpiryDate, nowMs);
  const share = elapsedShare(latestVisa?.issue_date, visaExpiryDate, nowMs);
  const permitName = latestVisa ? permitLine(documents) : null;
  const address = client.address?.trim();
  const ocrBusy = ocrPolling || isExtracting;

  // Same download flow as the passport: through the backend proxy, never
  // rendered on the page.
  const handleDownload = (e: React.MouseEvent) => {
    e.preventDefault();
    if (!latestVisaFileUrl) return;
    const fileId = extractDriveFileId(latestVisaFileUrl);
    const href = fileId
      ? `/api/documents/proxy/${fileId}`
      : safeExternalUrl(latestVisaFileUrl);
    if (!href) return;
    const link = document.createElement("a");
    link.href = href;
    link.download = `visa_${client.full_name?.replace(/\s+/g, "_") || "document"}.pdf`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // OCR is only ever started by the explicit "Extract dates" action: opening a
  // client must never send a document to OCR (the backend overwrites the
  // client's visa fields with whatever it reads).
  const handleExtractVisa = async () => {
    if (!latestVisa?.google_drive_file_url || isExtracting) return;
    const fileId = extractDriveFileId(latestVisa.google_drive_file_url);
    if (!fileId) return;
    setIsExtracting(true);
    try {
      const response = (await api.post(
        `/api/crm/clients/${clientId}/extract-visa`,
        {
          file_id: fileId,
          doc_id: latestVisa.id,
        },
      )) as {
        success: boolean;
        extracted?: {
          expiry_date?: string;
          issue_date?: string;
          visa_type?: string;
        };
      };
      if (response.success && response.extracted) {
        const details = [];
        if (response.extracted.visa_type)
          details.push(`Type: ${response.extracted.visa_type}`);
        if (response.extracted.issue_date)
          details.push(`Issue: ${response.extracted.issue_date}`);
        if (response.extracted.expiry_date)
          details.push(`Expiry: ${response.extracted.expiry_date}`);
        if (details.length > 0) {
          toast.success("Visa data extracted!", {
            description: details.join(" | "),
          });
        }
        await onRefresh();
      }
    } catch (err) {
      logger.error("Visa OCR failed", { metadata: { error: String(err) } });
      toast.error("OCR failed. Please try again.");
    } finally {
      setIsExtracting(false);
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // Validate file type
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

    // Base64 through the proxy: see lib/upload/base64UploadLimit
    if (file.size > BASE64_UPLOAD_MAX_BYTES) {
      toast.error("File too large", {
        description: `Maximum file size is ${BASE64_UPLOAD_MAX_LABEL}`,
      });
      return;
    }

    setIsUploading(true);
    try {
      // Convert file to base64 using utility function
      const base64 = await fileToBase64(file);

      const response = (await api.post(
        `/api/crm/clients/${client.id}/documents/upload`,
        {
          file: base64,
          file_name: file.name,
          document_type: "visa",
          mime_type: file.type,
          subfolder_hint: "Actual Visa",
          document_category: "immigration",
        },
      )) as {
        success: boolean;
        message?: string;
      };

      if (response.success) {
        toast.success("Visa uploaded — OCR in corso...");
        pollOcrStatus();
      } else {
        toast.error("Upload failed", { description: response.message });
      }
    } catch (err) {
      toast.error("Upload failed", { description: (err as Error).message });
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  };

  const doDelete = async () => {
    if (!latestVisa) return;
    setIsDeleting(true);
    try {
      await api.request(`/api/crm/documents/${latestVisa.id}`, {
        method: "DELETE",
      });
      toast.success("Visa deleted");
      await onRefresh();
    } catch (err) {
      toast.error("Delete failed", { description: (err as Error).message });
    } finally {
      setIsDeleting(false);
    }
  };

  const handleDelete = () => {
    if (!latestVisa) return;
    toast("Delete visa document? This will mark it as deleted.", {
      action: { label: "Delete", onClick: () => void doDelete() },
      cancel: { label: "Cancel", onClick: () => toast.dismiss() },
    });
  };

  return (
    <CardFrame
      tone="forest"
      kicker="Stay permit"
      title="Visa"
      art="visa"
      testId="visa-card"
      headerExtra={
        ocrBusy ? (
          <span
            role="status"
            className="inline-flex items-center gap-1.5 text-[12px] text-[var(--state-success)]"
          >
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            Reading the scan…
          </span>
        ) : undefined
      }
      actions={
        <>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/jpeg,image/jpg,image/png,application/pdf"
            aria-label="Upload visa document"
            onChange={handleFileUpload}
            className="hidden"
          />
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5 border-[var(--line-control)]"
            onClick={handleDownload}
            disabled={!latestVisaFileUrl}
            aria-label="Download visa"
          >
            <Download className="h-4 w-4" aria-hidden="true" />
            Download
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5 border-[var(--line-control)]"
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading}
          >
            {isUploading ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Upload className="h-4 w-4" aria-hidden="true" />
            )}
            {isUploading ? "Uploading…" : "Upload"}
          </Button>
          {latestVisa && latestVisaFileUrl ? (
            <span className="ml-auto flex items-center gap-1">
              {!visaExpiryDate ? (
                <Button
                  variant="ghost"
                  size="sm"
                  className="min-h-8 gap-1.5 text-[var(--tx-secondary)]"
                  onClick={() => void handleExtractVisa()}
                  disabled={isExtracting}
                  aria-label="Extract dates from the visa scan"
                  data-testid="visa-extract-dates"
                >
                  {isExtracting ? (
                    <Loader2
                      className="h-4 w-4 animate-spin"
                      aria-hidden="true"
                    />
                  ) : (
                    <FileText className="h-4 w-4" aria-hidden="true" />
                  )}
                  {isExtracting ? "Extracting…" : "Extract dates"}
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0 text-[var(--tx-secondary)]"
                onClick={handleDelete}
                disabled={isDeleting}
                aria-label="Delete visa"
                title="Delete visa"
              >
                {isDeleting ? (
                  <Loader2
                    className="h-4 w-4 animate-spin"
                    aria-hidden="true"
                  />
                ) : (
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                )}
              </Button>
            </span>
          ) : null}
        </>
      }
    >
      {latestVisa ? (
        <>
          {permitName ? (
            <p
              className="mb-3.5 text-[17px] font-semibold leading-[1.3] text-[var(--tx-pure)]"
              data-testid="visa-permit-name"
            >
              {permitName}
            </p>
          ) : null}
          <Facts>
            <Fact label="Issued">
              {visaStartDate ? formatDate(visaStartDate) : NOT_RECORDED}
            </Fact>
            <Fact label="Expires">
              {visaExpiryDate ? (
                <span className="flex flex-wrap items-baseline gap-x-2">
                  <span>{formatDate(visaExpiryDate)}</span>
                  <ExpiryText days={expiryDays} />
                </span>
              ) : (
                NOT_RECORDED
              )}
            </Fact>
            {latestVisa.permit_sponsor ? (
              <Fact label="Sponsor" wide>
                {latestVisa.permit_sponsor}
              </Fact>
            ) : null}
          </Facts>
          {share !== null && visaExpiryDate ? (
            <div className="mt-4">
              <Runway share={share} warn={expiryTone(expiryDays) !== "ok"} />
            </div>
          ) : null}
        </>
      ) : visaProcess ? (
        <Facts>
          <Fact label="Status" wide>
            <span className="text-[var(--state-info)]">
              Visa in process · {humanize(visaProcess.status)}
            </span>
          </Fact>
          {visaProcess.start_date ? (
            <Fact label="Started">{formatDate(visaProcess.start_date)}</Fact>
          ) : null}
          <Fact label="Expected finish">
            {visaProcess.completion_date
              ? formatDate(visaProcess.completion_date)
              : "To be confirmed"}
          </Fact>
        </Facts>
      ) : (
        <p className="text-[13px] leading-[1.5] text-[var(--tx-secondary)]">
          No visa or KITAS on file. Upload the permit (JPG, PNG or PDF, up to{" "}
          {BASE64_UPLOAD_MAX_LABEL}) and its dates are read from it.
        </p>
      )}
      <div
        className={
          address
            ? "mt-4 border-t border-[var(--bz-border)] pt-3"
            : "mt-4 rounded-lg border border-[var(--state-warning)] px-3 py-2.5"
        }
        data-testid="visa-address"
      >
        <p className="text-[12px] text-[var(--tx-secondary)]">
          Registered address
        </p>
        {address ? (
          <p className="mt-0.5 break-words text-[14px] leading-[1.45] text-[var(--tx-pure)]">
            {address}
          </p>
        ) : (
          <p className="mt-0.5 text-[13px] font-semibold text-[var(--state-warning)]">
            No address on file — needed for the permit
          </p>
        )}
      </div>
    </CardFrame>
  );
}

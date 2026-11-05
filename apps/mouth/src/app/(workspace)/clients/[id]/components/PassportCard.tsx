"use client";

import React, { useState, useRef, useCallback, useEffect } from "react";
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
import {
  extractDriveFileId,
  getDriveProxyUrl,
  getPassportValidityColor,
  pickCurrentPassport,
  downloadHrefFor,
} from "./utils";
import { daysUntil } from "./overview-brief";
import { CardFrame, ExpiryText, Fact, Facts, NOT_RECORDED } from "./cardKit";

export function PassportCard({
  client,
  documents,
  formatDate,
  onRefresh,
  clientId,
}: {
  client: ClientProfile["client"];
  documents: ClientDocument[];
  formatDate: (d: string) => string;
  onRefresh: () => Promise<void>;
  clientId: number;
}) {
  const [isExtracting, setIsExtracting] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [ocrError, setOcrError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Poll OCR status after upload/extract
  const { ocrPolling, pollOcrStatus } = useOcrPolling({
    clientId,
    onDone: onRefresh,
  });

  // The client's own current passport — see pickCurrentPassport.
  const passportDoc = pickCurrentPassport(documents);

  // Get passport validity color and alert level
  const passportValidity = getPassportValidityColor(client.passport_expiry);
  // The scan is only ever FETCHED on demand (Download, Extract) — the card
  // never renders it, so opening the Overview loads no document bytes.
  const passportImageUrl = passportDoc?.google_drive_file_url;
  const passportIsPdf =
    passportDoc?.file_name?.toLowerCase().endsWith(".pdf") ||
    passportImageUrl?.toLowerCase().includes(".pdf") ||
    passportDoc?.file_name?.toLowerCase().includes(".pdf");

  const expiryDays = daysUntil(client.passport_expiry, Date.now());

  // Convert Drive view URL to direct download URL
  const getDownloadUrl = downloadHrefFor;

  const handleDownload = (e: React.MouseEvent) => {
    e.preventDefault();
    if (passportImageUrl) {
      const downloadUrl = getDownloadUrl(passportImageUrl);
      if (!downloadUrl) return;
      const link = document.createElement("a");
      link.href = downloadUrl;
      link.download = `passport_${client.full_name?.replace(/\s+/g, "_") || "document"}.pdf`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
  };

  const isExtractingRef = useRef(false);

  const blobToBase64 = async (blob: Blob): Promise<string> =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result as string;
        resolve(result.split(",")[1] ?? result);
      };
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });

  // Enhanced OCR extraction with Gemini Vision
  const handleExtractData = useCallback(async () => {
    if (!passportImageUrl || isExtractingRef.current) return;
    const fileId = extractDriveFileId(passportImageUrl);
    if (!fileId) {
      toast.error("Invalid document URL");
      return;
    }
    setIsExtracting(true);
    isExtractingRef.current = true;
    setOcrError(null);
    try {
      const proxyUrl = getDriveProxyUrl(passportImageUrl, "full");
      if (!proxyUrl) {
        throw new Error("Passport document cannot be proxied for OCR");
      }

      const fileResponse = await fetch(proxyUrl);
      if (!fileResponse.ok) {
        throw new Error(`Passport download failed (${fileResponse.status})`);
      }

      const blob = await fileResponse.blob();
      const imageBase64 = await blobToBase64(blob);
      const mimeType =
        blob.type || (passportIsPdf ? "application/pdf" : "image/jpeg");
      const response = await api.crm.extractPassportForClient(
        imageBase64,
        mimeType,
        client.id,
      );

      if (response.success) {
        const details = [];
        if (response.passport_number)
          details.push(`Passport: ${response.passport_number}`);
        if (response.passport_expiry)
          details.push(`Expiry: ${response.passport_expiry}`);
        if (response.gender) details.push(`Gender: ${response.gender}`);
        if (response.birthplace)
          details.push(`Birthplace: ${response.birthplace}`);
        if (response.name_match === false) {
          toast.warning("Name mismatch", {
            description: "Passport name differs from client record",
          });
        }
        toast.success("Passport data extracted!", {
          description: details.join(" | "),
        });
        await onRefresh();
      } else {
        toast.warning("OCR failed", {
          description: response.message || "Could not extract passport data",
        });
      }
    } catch (err) {
      logger.error("Passport OCR failed", { metadata: { error: String(err) } });
      setOcrError("OCR failed. Click to retry.");
      toast.error("Extraction failed", { description: (err as Error).message });
    } finally {
      setIsExtracting(false);
      isExtractingRef.current = false;
    }
  }, [passportImageUrl, passportIsPdf, client.id, onRefresh]);

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
          document_type: "passport",
          mime_type: file.type,
        },
      )) as {
        success: boolean;
        message?: string;
      };

      if (response.success) {
        toast.success("Passport uploaded — OCR in corso...");
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
    if (!passportDoc) return;
    setIsDeleting(true);
    try {
      await api.request(`/api/crm/documents/${passportDoc.id}`, {
        method: "DELETE",
      });
      toast.success("Passport deleted");
      await onRefresh();
    } catch (err) {
      toast.error("Delete failed", { description: (err as Error).message });
    } finally {
      setIsDeleting(false);
    }
  };

  const handleDelete = () => {
    if (!passportDoc) return;
    toast("Delete passport document? This will mark it as deleted.", {
      action: { label: "Delete", onClick: () => void doDelete() },
      cancel: { label: "Cancel", onClick: () => toast.dismiss() },
    });
  };

  const hasData = Boolean(
    client.passport_number ||
    client.passport_expiry ||
    client.date_of_birth ||
    client.nationality,
  );
  const ocrBusy = ocrPolling || isExtracting;

  return (
    <CardFrame
      tone="slate"
      kicker="Identity document"
      title="Passport"
      art="passport"
      testId="passport-card"
      headerExtra={
        ocrBusy ? (
          <span
            role="status"
            className="inline-flex items-center gap-1.5 text-[12px] text-[var(--state-info)]"
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
            aria-label="Upload passport"
            onChange={handleFileUpload}
            className="hidden"
          />
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5 border-[var(--line-control)]"
            onClick={handleDownload}
            disabled={!passportImageUrl}
            aria-label="Download passport"
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
          {passportImageUrl ? (
            <span className="ml-auto flex items-center gap-1">
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 text-[var(--tx-secondary)]"
                onClick={handleExtractData}
                disabled={isExtracting}
              >
                {isExtracting ? (
                  <Loader2
                    className="h-4 w-4 animate-spin"
                    aria-hidden="true"
                  />
                ) : (
                  <FileText className="h-4 w-4" aria-hidden="true" />
                )}
                {isExtracting ? "Extracting…" : "Extract"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0 text-[var(--tx-secondary)]"
                onClick={handleDelete}
                disabled={isDeleting}
                aria-label="Delete passport"
                title="Delete passport"
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
      {hasData ? (
        <Facts>
          <Fact label="Passport number">
            {client.passport_number ? (
              <span
                className="font-mono text-[13px]"
                style={{ fontVariantNumeric: "tabular-nums" }}
              >
                {client.passport_number}
              </span>
            ) : (
              NOT_RECORDED
            )}
          </Fact>
          <Fact label="Nationality">{client.nationality || NOT_RECORDED}</Fact>
          <Fact label="Date of birth">
            {client.date_of_birth
              ? formatDate(client.date_of_birth)
              : NOT_RECORDED}
          </Fact>
          <Fact label="Place of birth">
            {client.birthplace || NOT_RECORDED}
          </Fact>
          {passportDoc?.issue_date ? (
            <Fact label="Issued">{formatDate(passportDoc.issue_date)}</Fact>
          ) : null}
          <Fact label="Expires" wide={!passportDoc?.issue_date}>
            {client.passport_expiry ? (
              <span className="flex flex-wrap items-baseline gap-x-2">
                <span>{formatDate(client.passport_expiry)}</span>
                <ExpiryText days={expiryDays} />
              </span>
            ) : (
              NOT_RECORDED
            )}
          </Fact>
        </Facts>
      ) : (
        <p className="text-[13px] leading-[1.5] text-[var(--tx-secondary)]">
          No passport on file. Upload a scan (JPG, PNG or PDF, up to{" "}
          {BASE64_UPLOAD_MAX_LABEL}) and the details are read from it.
        </p>
      )}
      {client.passport_expiry && passportValidity.alertLevel !== "ok" ? (
        <p
          className="mt-3 text-[12px] font-semibold text-[var(--state-warning)]"
          data-testid="passport-alert"
        >
          {passportValidity.alertLevel === "warning"
            ? "Inside the 13-month window: contact the embassy soon."
            : passportValidity.alertLevel === "expired"
              ? "This passport has expired: renewal comes first."
              : "Expires very soon: contact the embassy now."}
        </p>
      ) : null}
      {ocrError ? (
        <p className="mt-2 text-[12px] text-[var(--state-warning)]">
          {ocrError}
        </p>
      ) : null}
      {passportDoc?.file_name ? (
        <p
          className="mt-3 truncate text-[12px] text-[var(--tx-secondary)]"
          data-testid="passport-file"
          title={passportDoc.file_name}
        >
          Scan on file: {passportDoc.file_name}
        </p>
      ) : null}
    </CardFrame>
  );
}

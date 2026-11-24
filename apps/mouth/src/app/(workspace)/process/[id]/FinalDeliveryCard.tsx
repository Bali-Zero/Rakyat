"use client";

import { useId, useRef, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronDown,
  Circle,
  FileCheck2,
  Loader2,
  UploadCloud,
} from "lucide-react";
import styles from "./FinalDeliveryCard.module.css";
import useSWR from "swr";
import { api } from "@/lib/api";
import type { Practice } from "@/lib/api/crm/crm.types";
import {
  isUnconfiguredDelivery,
  type DeliverySlot as Slot,
} from "@/lib/api/crm/delivery-slot";
import { fileToBase64 } from "@/lib/utils";
import {
  validStaffText,
  VisaPermitFollowup,
  type VisaFollowup,
  type FollowupAction,
} from "./VisaPermitFollowup";
import {
  acceptResponse,
  isSameIdentity,
  readSessionIdentity,
  useSessionIdentity,
  type SessionIdentity,
} from "@/lib/auth/session-identity";
import { workDayOf } from "@/lib/format/work-day";
import {
  BASE64_UPLOAD_MAX_BYTES,
  BASE64_UPLOAD_MAX_LABEL,
} from "@/lib/upload/base64UploadLimit";
import { UPLOAD_ACCEPT, isAllowedUploadMime } from "@/lib/vault/uploadLimits";

interface Delivery {
  status: string;
  bonus_slot: string | null;
  bonus_status?: string | null;
  bonus_event:
    | "electronic_visa_issued"
    | "kitas_imk_uploaded"
    | "service_delivered"
    | null;
  visa_followup?: VisaFollowup | null;
  needs_titles: boolean;
  slots: Slot[];
}

const actionStyle =
  "text-sm font-semibold text-[var(--bz-accent)] underline disabled:opacity-50";

type DeliveryCardProps = { practice: Practice; onComplete: () => void };

export function FinalDeliveryCard(props: DeliveryCardProps) {
  const identity = useSessionIdentity();
  if (!identity) return null;
  return (
    <StaffDeliveryCard
      key={JSON.stringify([props.practice.id, identity.sub, identity.epoch])}
      {...props}
      identity={identity}
    />
  );
}

function StaffDeliveryCard({
  practice,
  onComplete,
  identity,
}: DeliveryCardProps & { identity: SessionIdentity }) {
  const url = `/api/crm/practices/${practice.id}/delivery`;
  const controlId = useId();
  async function boundRequest<T>(
    path: string,
    body?: unknown,
    post = false,
  ): Promise<T> {
    if (!isSameIdentity(readSessionIdentity(), identity))
      throw new Error("Session changed. Reload the practice.");
    const response = await api.requestWithCredential<T>(
      path,
      post
        ? {
            method: "POST",
            body: body === undefined ? undefined : JSON.stringify(body),
          }
        : undefined,
    );
    if (!acceptResponse(response, identity))
      throw new Error("Session changed. Reload the practice.");
    return response.data;
  }
  const post = <T,>(path: string, body?: unknown) =>
    boundRequest<T>(path, body, true);
  const { data, error, mutate } = useSWR<Delivery>(
    [url, identity.sub, identity.epoch],
    () => boundRequest<Delivery>(url),
    { shouldRetryOnError: false },
  );
  const [working, setWorking] = useState(false);
  const [uploadingKey, setUploadingKey] = useState<string | null>(null);
  const [issueDate, setIssueDate] = useState("");
  const today = workDayOf();
  const validIssueDate =
    /^\d{4}-\d{2}-\d{2}$/.test(issueDate) && issueDate <= today;
  const pending = useRef(false);
  const [feedback, setFeedback] = useState("");
  const [failure, setFailure] = useState("");
  const [titles, setTitles] = useState("");
  const [waiveKey, setWaiveKey] = useState("");
  const [waiver, setWaiver] = useState("");
  const [chooseCompany, setChooseCompany] = useState(false);
  const [companyId, setCompanyId] = useState("");
  const { data: companies, error: companyError } = useSWR(
    chooseCompany
      ? ["delivery-companies", identity.sub, identity.epoch, practice.client_id]
      : null,
    () => api.crm.getClientCompanies(practice.client_id),
  );
  const writable =
    practice.can_manage_delivery === true &&
    data?.status === "on_process" &&
    practice.payment_status === "paid";
  const visaDateSlot = data?.bonus_slot === "electronic_visa";
  const bonusLabel = visaDateSlot ? "Electronic Visa" : "renewed IMK";
  const missingSlots =
    data?.slots.filter(
      (slot) =>
        slot.document_id == null && !(slot.condition && slot.waiver_reason),
    ) ?? [];
  const canDeliver =
    writable &&
    !data.needs_titles &&
    data.slots.length > 0 &&
    missingSlots.length === 0;
  const readyCount = (data?.slots.length ?? 0) - missingSlots.length;
  const canUpload =
    !writable ||
    !chooseCompany ||
    companies?.some((company) => String(company.company_id) === companyId);

  function repairable(slot?: Slot): boolean {
    return (
      practice.can_manage_delivery === true &&
      data?.status === "completed" &&
      slot?.can_repair === true
    );
  }

  async function change(
    request: () => Promise<Delivery>,
    message: string,
    repairSlot?: Slot,
  ) {
    if (pending.current || !(writable || repairable(repairSlot))) return;
    pending.current = true;
    setWorking(true);
    setFailure("");
    setFeedback("");
    try {
      const result = await request();
      await mutate(result, { revalidate: false });
      setFeedback(message);
      window.dispatchEvent(new Event("kita:delivery-changed"));
      setWaiveKey("");
      setWaiver("");
      if (writable && result.status === "completed") onComplete();
    } catch (e) {
      setFailure(
        e instanceof Error
          ? e.message
          : "The change could not be saved. Please retry.",
      );
      // A response can be lost after the server commits. Recover its actual state.
      const refreshed = await mutate().catch(() => undefined);
      if (writable && refreshed?.status === "completed") {
        setFailure("");
        setFeedback("Final documents delivered. Practice completed.");
        window.dispatchEvent(new Event("kita:delivery-changed"));
        onComplete();
      }
    } finally {
      pending.current = false;
      setWorking(false);
    }
  }

  function needsIssueDate(slot: Slot): boolean {
    return (
      visaDateSlot &&
      slot.key === data?.bonus_slot &&
      data.bonus_event !== "electronic_visa_issued"
    );
  }

  function attach(slot: Slot, file: File) {
    if (pending.current) return;
    if (needsIssueDate(slot) && !validIssueDate) {
      setFailure(
        "Enter the issue date printed on the Electronic Visa, up to today in Bali.",
      );
      return;
    }
    if (
      !file.size ||
      file.size > BASE64_UPLOAD_MAX_BYTES ||
      !isAllowedUploadMime(file.type)
    ) {
      setFailure(
        `Choose a PDF, JPG, PNG or DOCX file up to ${BASE64_UPLOAD_MAX_LABEL}.`,
      );
      return;
    }
    setUploadingKey(slot.key);
    void change(
      async () =>
        post<Delivery>(
          `${url}/slots/${encodeURIComponent(slot.key)}${repairable(slot) ? "/repair" : ""}`,
          {
            file_name: file.name,
            ...(needsIssueDate(slot) ? { issued_on: issueDate } : {}),
            file_content: await fileToBase64(file),
            mime_type: file.type,
            ...(writable && chooseCompany && companyId
              ? { company_id: Number(companyId) }
              : {}),
          },
        ),
      repairable(slot)
        ? "Missing permit restored."
        : slot.key === data?.bonus_slot
          ? `${visaDateSlot ? "Electronic Visa" : "Renewed IMK"} upload recorded.`
          : "Document saved.",
      slot,
    ).finally(() => setUploadingKey(null));
  }

  async function followupAction(action: FollowupAction, note: string) {
    if (!data) return;
    await change(
      async () => {
        const next = await post<VisaFollowup>(
          `${url}/visa-followup/${action}`,
          action === "confirm-receipt" ? { reference: note } : { note },
        );
        return { ...data, visa_followup: next };
      },
      action === "postpone"
        ? "Postponement recorded."
        : action === "confirm-receipt"
          ? "Existing email receipt recorded."
          : "Escalation status updated.",
      data.slots.find((s) => s.key === data.visa_followup?.slot_key),
    );
  }

  return (
    <section
      id="final-delivery"
      className={styles.card}
      aria-label="Final delivery"
    >
      <header className={styles.header}>
        <span className={styles.headerIcon} aria-hidden>
          <FileCheck2 size={22} />
        </span>
        <div className="min-w-0">
          <h2>
            {data?.status === "completed"
              ? "Final documents delivered"
              : writable
                ? "Upload final documents"
                : "Final documents"}
          </h2>
          <p>
            {writable
              ? "Upload the issued documents listed below, then mark this practice as completed."
              : practice.can_manage_delivery === false
                ? "View-only access. The assigned operator manages uploads and completion."
                : "Review the final documents for this practice."}
          </p>
        </div>
        {data && !error && !data.needs_titles && data.slots.length > 0 && (
          <span className={styles.progress}>
            {readyCount} / {data.slots.length} requirements ready
          </span>
        )}
      </header>
      <div className={styles.body}>
        {isUnconfiguredDelivery(error) ? (
          <p>No delivery checklist is configured for this service.</p>
        ) : (
          error && (
            <p role="alert">
              Unable to load delivery requirements.{" "}
              <button className={actionStyle} onClick={() => void mutate()}>
                Retry
              </button>
            </p>
          )
        )}
        {!data && !error && <p>Loading delivery requirements…</p>}
        {data && !error && (
          <>
            {data.bonus_slot && (
              <p className="mb-3 text-sm" role="status">
                {data.bonus_status === "ineligible"
                  ? "The qualifying upload is recorded. The assignee is ineligible for a payroll bonus."
                  : data.bonus_event
                    ? `Bonus recorded on the ${bonusLabel} upload. Remaining documents do not earn another bonus.`
                    : `The bonus accrues when you upload the ${bonusLabel}.${visaDateSlot ? " Enter the printed issue date for the document record." : ""}`}
              </p>
            )}
            {writable && !practice.family_member_id && (
              <details className={styles.company}>
                <summary>
                  Company filing (optional){chooseCompany && " · enabled"}
                  <ChevronDown size={15} aria-hidden />
                </summary>
                <p>
                  Use this only for a document issued to a linked company.
                  Otherwise it is saved under the client on this practice.
                </p>
                <label className={styles.checkbox}>
                  <input
                    type="checkbox"
                    checked={chooseCompany}
                    disabled={working}
                    onChange={(e) => {
                      setChooseCompany(e.target.checked);
                      setCompanyId("");
                    }}
                  />{" "}
                  Save uploads under a linked company
                </label>
                {chooseCompany && (
                  <label className="block mt-1">
                    Company for these documents
                    <select
                      className={styles.select}
                      disabled={working}
                      value={companyId}
                      onChange={(e) => setCompanyId(e.target.value)}
                    >
                      <option value="">Select a company</option>
                      {companies?.map((c) => (
                        <option key={c.company_id} value={c.company_id}>
                          {c.company_name}
                        </option>
                      ))}
                    </select>
                    {companyError && (
                      <span role="alert">Unable to load linked companies.</span>
                    )}
                  </label>
                )}
              </details>
            )}
            {writable && (
              <p className={styles.uploadHelp}>
                Choose the final file for each requirement. Each file saves
                automatically when selected.
              </p>
            )}
            {writable && data.needs_titles && (
              <form
                className="mb-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  const values = titles
                    .split("\n")
                    .map((t) => t.trim())
                    .filter(Boolean);
                  if (values.length)
                    void change(
                      () => post<Delivery>(`${url}/titles`, { titles: values }),
                      "Document titles saved.",
                    );
                }}
              >
                <label>
                  Agreed final documents, one title per line
                  <textarea
                    required
                    value={titles}
                    onChange={(e) => setTitles(e.target.value)}
                    disabled={working}
                    className="block w-full rounded border p-2"
                  />
                </label>
                <button
                  className={actionStyle}
                  disabled={working || !titles.trim()}
                >
                  Save document titles
                </button>
              </form>
            )}
            {!data.needs_titles && (
              <ul className={styles.slots}>
                {data.slots.map((slot) => (
                  <li
                    key={slot.key}
                    className={styles.slot}
                    data-ready={
                      slot.document_id != null ||
                      Boolean(slot.condition && slot.waiver_reason)
                    }
                    aria-busy={uploadingKey === slot.key}
                  >
                    <div className={styles.slotHeading}>
                      {slot.document_id != null ||
                      (slot.condition && slot.waiver_reason) ? (
                        <CheckCircle2 size={19} aria-hidden />
                      ) : (
                        <Circle size={19} aria-hidden />
                      )}
                      <h3>{slot.label}</h3>
                      <span>
                        {slot.document_id != null
                          ? "Saved"
                          : slot.condition && slot.waiver_reason
                            ? "Not applicable"
                            : slot.condition
                              ? "If applicable"
                              : "Required"}
                      </span>
                    </div>
                    {slot.condition && (
                      <p className="text-sm text-[var(--bz-text-2)]">
                        {slot.condition}
                      </p>
                    )}
                    {slot.document_id != null ? (
                      <p className={styles.fileName}>
                        <Check size={14} aria-hidden />
                        {slot.file_name || "Document saved"}
                      </p>
                    ) : slot.waiver_reason ? (
                      <p className={styles.fileName}>
                        Recorded reason · {slot.waiver_reason}
                      </p>
                    ) : (
                      <p className="text-sm text-[var(--bz-text-2)]">
                        {writable || repairable(slot)
                          ? "Upload the issued document that confirms this service is finished."
                          : "This document has not been uploaded yet."}
                      </p>
                    )}
                    {data.visa_followup?.slot_key === slot.key && (
                      <VisaPermitFollowup
                        practiceId={practice.id}
                        value={data.visa_followup}
                        busy={working}
                        writable={Boolean(writable)}
                        act={followupAction}
                      />
                    )}
                    {visaDateSlot &&
                      slot.key === data.bonus_slot &&
                      slot.issued_on && (
                        <p className="text-sm">
                          Electronic Visa issue date ·{" "}
                          <time dateTime={slot.issued_on}>
                            {slot.issued_on}
                          </time>
                        </p>
                      )}
                    {writable && needsIssueDate(slot) && (
                      <div>
                        <label
                          htmlFor={`${controlId}-issue-date`}
                          className="block text-sm font-medium"
                        >
                          Electronic Visa issue date
                        </label>
                        <input
                          id={`${controlId}-issue-date`}
                          type="date"
                          required
                          max={today}
                          value={issueDate}
                          disabled={working}
                          aria-describedby={`${controlId}-issue-help`}
                          onChange={(e) => setIssueDate(e.currentTarget.value)}
                          className={styles.select}
                        />
                        <p
                          id={`${controlId}-issue-help`}
                          className={styles.reason}
                        >
                          Enter the issue date printed on the Electronic Visa to
                          enable upload.
                        </p>
                      </div>
                    )}
                    {(writable || repairable(slot)) && (
                      <>
                        <div className={styles.uploadRow}>
                          <label
                            className={styles.uploadButton}
                            data-disabled={
                              working ||
                              !canUpload ||
                              (needsIssueDate(slot) && !validIssueDate)
                            }
                          >
                            {uploadingKey === slot.key ? (
                              <Loader2
                                size={17}
                                className="motion-safe:animate-spin"
                                aria-hidden
                              />
                            ) : (
                              <UploadCloud size={17} aria-hidden />
                            )}
                            <span>
                              {uploadingKey === slot.key
                                ? "Saving document…"
                                : repairable(slot)
                                  ? "Restore document"
                                  : slot.document_id != null
                                    ? "Replace document"
                                    : "Upload document"}
                            </span>
                            <input
                              className="sr-only"
                              type="file"
                              aria-label={`${repairable(slot) ? "Restore document" : slot.document_id == null ? "Upload document" : "Replace document"} — ${slot.label}`}
                              aria-describedby={`${controlId}-formats`}
                              accept={UPLOAD_ACCEPT}
                              disabled={
                                working ||
                                !canUpload ||
                                (needsIssueDate(slot) && !validIssueDate)
                              }
                              onChange={(e) => {
                                const file = e.currentTarget.files?.[0];
                                e.currentTarget.value = "";
                                if (file) attach(slot, file);
                              }}
                            />
                          </label>
                          {chooseCompany && !canUpload && (
                            <p className={styles.reason}>
                              Select a linked company above to enable upload.
                            </p>
                          )}
                          {repairable(slot) && (
                            <p className="text-xs text-[var(--bz-text-2)]">
                              Restore the missing permit on this completed
                              practice.
                            </p>
                          )}
                        </div>
                        {slot.reusable_document_id != null &&
                          !slot.document_id && (
                            <button
                              type="button"
                              className={actionStyle}
                              disabled={working}
                              onClick={() =>
                                void change(
                                  () =>
                                    post<Delivery>(
                                      `${url}/slots/${encodeURIComponent(slot.key)}/reuse`,
                                      {
                                        document_id: slot.reusable_document_id,
                                      },
                                    ),
                                  "Combined permit linked to both requirements.",
                                  slot,
                                )
                              }
                            >
                              Confirm the existing permit covers both documents
                            </button>
                          )}
                        {writable &&
                          slot.condition &&
                          !slot.document_id &&
                          !slot.waiver_reason &&
                          (waiveKey === slot.key ? (
                            <form
                              onSubmit={(e) => {
                                e.preventDefault();
                                if (!validStaffText(waiver, 500)) return;
                                void change(
                                  () =>
                                    post<Delivery>(
                                      `${url}/slots/${encodeURIComponent(slot.key)}/waive`,
                                      { reason: waiver.trim() },
                                    ),
                                  "Requirement updated.",
                                );
                              }}
                            >
                              <label className="block text-sm">
                                Reason this requirement does not apply
                                <input
                                  className="block w-full rounded border p-1"
                                  required
                                  disabled={working}
                                  value={waiver}
                                  onChange={(e) => setWaiver(e.target.value)}
                                />
                              </label>
                              <button
                                className={actionStyle}
                                disabled={
                                  working || !validStaffText(waiver, 500)
                                }
                              >
                                Save reason
                              </button>
                            </form>
                          ) : (
                            <button
                              className={actionStyle}
                              disabled={working}
                              onClick={() => {
                                setWaiver("");
                                setWaiveKey(slot.key);
                              }}
                            >
                              Mark as not applicable
                            </button>
                          ))}
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {(writable || data.slots.some(repairable)) && (
              <p id={`${controlId}-formats`} className={styles.formats}>
                PDF, JPG, PNG or DOCX · Up to {BASE64_UPLOAD_MAX_LABEL} per file
              </p>
            )}
            {writable && (
              <footer className={styles.complete}>
                <div>
                  <h3>
                    {canDeliver
                      ? "Ready to complete"
                      : "What is needed to complete"}
                  </h3>
                  <p id={`${controlId}-complete-help`}>
                    {working
                      ? "Wait for the current action to finish."
                      : data.needs_titles
                        ? "Save the agreed document names, then upload their files."
                        : !data.slots.length
                          ? "The final document requirements must be defined first."
                          : missingSlots.length
                            ? `Upload ${missingSlots.map((slot) => slot.label).join(", ")} before completing this practice.`
                            : "All document requirements are ready. Complete the practice to record delivery."}
                  </p>
                  {missingSlots.some((slot) => slot.condition) && (
                    <p>
                      For a requirement marked If applicable, you can record why
                      it does not apply.
                    </p>
                  )}
                  <p className={styles.consequence}>
                    Completing changes the status to Completed and publishes the
                    final documents for practices shared with the client.
                  </p>
                </div>
                <button
                  className={styles.completeButton}
                  disabled={working || !canDeliver}
                  aria-describedby={`${controlId}-complete-help`}
                  onClick={() =>
                    void change(
                      () => post<Delivery>(`${url}/complete`),
                      "Final documents delivered. Practice completed.",
                    )
                  }
                >
                  {working ? (
                    <Loader2
                      size={17}
                      className="motion-safe:animate-spin"
                      aria-hidden
                    />
                  ) : (
                    <CheckCircle2 size={17} aria-hidden />
                  )}
                  Mark practice as completed
                </button>
              </footer>
            )}
          </>
        )}
        {feedback && (
          <p role="status" className={styles.feedback}>
            {feedback}
          </p>
        )}
        {failure && (
          <p role="alert" className={styles.failure}>
            {failure}
          </p>
        )}
      </div>
    </section>
  );
}

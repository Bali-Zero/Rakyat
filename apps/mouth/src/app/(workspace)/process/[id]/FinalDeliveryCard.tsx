"use client";

import { useId, useRef, useState } from "react";
import { CheckCircle2, ChevronDown, Loader2, UploadCloud } from "lucide-react";
import styles from "./FinalDeliveryCard.module.css";
import useSWR from "swr";
import { api } from "@/lib/api";
import type { Practice } from "@/lib/api/crm/crm.types";
import {
  isUnconfiguredDelivery,
  deliveryDocumentTitle,
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
  bonus_rate?: number | null;
  bonus_event:
    | "electronic_visa_issued"
    | "kitas_imk_uploaded"
    | "service_delivered"
    | null;
  visa_followup?: VisaFollowup | null;
  needs_titles: boolean;
  // A November practice completed before Final delivery: it takes every empty document.
  legacy_proof?: boolean;
  bonus_instruction?: "upload" | "upload_or_waive" | null;
  slots: Slot[];
}

function bonusInstruction(data: Delivery, kind: "upload" | "upload_or_waive") {
  const slot = data.slots.find((s) => s.key === data.bonus_slot);
  const label = slot ? deliveryDocumentTitle(slot) : "required document";
  return kind === "upload_or_waive"
    ? `Upload the ${label}, or mark it not applicable before uploading another document, to record the bonus.`
    : `The ${label} is required to record the bonus.`;
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
  const legacyOpen =
    practice.can_manage_delivery === true &&
    data?.status === "completed" &&
    data.legacy_proof === true;
  const staffEdit = writable || legacyOpen;
  const visaDateSlot = data?.bonus_slot === "electronic_visa";
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
    // An upload is authorised by its own slot; the titles and waiver forms of a cohort
    // practice carry no slot and are authorised by the staff-edit permission.
    const allowed =
      writable ||
      repairable(repairSlot) ||
      (legacyOpen && repairSlot === undefined);
    if (pending.current || !allowed) return;
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
      repairable(slot) && !legacyOpen
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
      // A cohort practice is authorised by staff-edit; the server judges each action.
      legacyOpen
        ? undefined
        : data.slots.find((s) => s.key === data.visa_followup?.slot_key),
    );
  }

  const displayedSlots = [...(data?.slots ?? [])].sort(
    (a, b) =>
      Number(deliveryDocumentTitle(b) === "LKPM submission proof") -
      Number(deliveryDocumentTitle(a) === "LKPM submission proof"),
  );

  return (
    <section
      id="final-delivery"
      className={styles.card}
      aria-label="Final delivery"
    >
      <h2 className="sr-only">Final delivery</h2>
      {isUnconfiguredDelivery(error) ? (
        <p>No delivery checklist is configured for this service.</p>
      ) : error ? (
        <p role="alert">
          Unable to load delivery requirements.{" "}
          <button className={actionStyle} onClick={() => void mutate()}>
            Retry
          </button>
        </p>
      ) : null}
      {!data && !error && <p role="status">Loading documents…</p>}
      {data && !error && (
        <>
          {practice.can_manage_delivery === false && (
            <p className={styles.hint}>View-only access</p>
          )}
          {staffEdit && data.needs_titles && (
            <form
              className={styles.custom}
              onSubmit={(event) => {
                event.preventDefault();
                const values = titles
                  .split("\n")
                  .map((title) => title.trim())
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
                  disabled={working}
                  value={titles}
                  onChange={(event) => setTitles(event.target.value)}
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
              {displayedSlots.map((slot) => {
                const title = deliveryDocumentTitle(slot);
                const editable = writable || repairable(slot);
                const saved = slot.document_id != null;
                const waived = Boolean(slot.condition && slot.waiver_reason);
                const disabled =
                  working ||
                  !canUpload ||
                  (needsIssueDate(slot) && !validIssueDate);
                const action = legacyOpen
                  ? "Upload"
                  : repairable(slot)
                    ? "Restore"
                    : saved
                      ? "Replace"
                      : "Upload";
                const timing = slot.label.split(" · ").slice(1).join(" · ");
                return (
                  <li
                    key={slot.key}
                    className={styles.slot}
                    aria-busy={uploadingKey === slot.key}
                  >
                    <label
                      className={styles.fileBox}
                      data-ready={saved || waived}
                      data-disabled={editable && disabled}
                      data-editable={editable}
                    >
                      {uploadingKey === slot.key ? (
                        <Loader2
                          className="motion-safe:animate-spin"
                          size={24}
                          aria-hidden
                        />
                      ) : saved || waived ? (
                        <CheckCircle2 size={24} aria-hidden />
                      ) : (
                        <UploadCloud size={24} aria-hidden />
                      )}
                      <strong>{title}</strong>
                      <span className={styles.fileName}>
                        {saved
                          ? slot.file_name || "File saved"
                          : waived
                            ? "Not applicable"
                            : uploadingKey === slot.key
                              ? "Saving…"
                              : editable
                                ? "Click to upload"
                                : "Awaiting document"}
                      </span>
                      {editable && (
                        <input
                          className="sr-only"
                          type="file"
                          accept={UPLOAD_ACCEPT}
                          aria-label={`${action} ${title}`}
                          aria-describedby={`${controlId}-format-${slot.key}`}
                          disabled={disabled}
                          onChange={(event) => {
                            const file = event.currentTarget.files?.[0];
                            event.currentTarget.value = "";
                            if (file) attach(slot, file);
                          }}
                        />
                      )}
                      {saved && editable && (
                        <span className={styles.replace} aria-hidden>
                          Click to replace
                        </span>
                      )}
                    </label>
                    <div className={styles.slotDetails}>
                      {editable && (
                        <p
                          id={`${controlId}-format-${slot.key}`}
                          className={styles.hint}
                        >
                          PDF, JPG, PNG, DOCX · Max {BASE64_UPLOAD_MAX_LABEL}
                        </p>
                      )}
                      {(slot.condition || timing) && (
                        <p className={styles.hint}>
                          {slot.condition || timing}
                        </p>
                      )}
                      {waived && (
                        <p className={styles.hint}>{slot.waiver_reason}</p>
                      )}
                      {slot.key === data.bonus_slot &&
                        data.bonus_event &&
                        !["ineligible", "forfeited", "no_bonus"].includes(
                          data.bonus_status ?? "",
                        ) && <p className={styles.saved}>Bonus recorded</p>}
                      {slot.key === data.bonus_slot && slot.issued_on && (
                        <p className={styles.hint}>
                          Issued{" "}
                          <time dateTime={slot.issued_on}>
                            {slot.issued_on}
                          </time>
                        </p>
                      )}
                      {editable && needsIssueDate(slot) && (
                        <div className={styles.issueDate}>
                          <label htmlFor={`${controlId}-issue-date`}>
                            Electronic Visa issue date
                          </label>
                          <input
                            id={`${controlId}-issue-date`}
                            type="date"
                            required
                            max={today}
                            value={issueDate}
                            disabled={working}
                            onChange={(event) =>
                              setIssueDate(event.target.value)
                            }
                          />
                          {!validIssueDate && (
                            <p className={styles.hint}>
                              Enter the issue date to enable upload.
                            </p>
                          )}
                        </div>
                      )}
                      {editable && chooseCompany && !canUpload && (
                        <p className={styles.hint}>
                          Select the company in Company filing to enable upload.
                        </p>
                      )}
                      {data.visa_followup?.slot_key === slot.key && (
                        <VisaPermitFollowup
                          practiceId={practice.id}
                          value={data.visa_followup}
                          busy={working}
                          writable={staffEdit}
                          act={followupAction}
                        />
                      )}
                      {editable &&
                        slot.reusable_document_id != null &&
                        !saved && (
                          <button
                            type="button"
                            className={actionStyle}
                            disabled={working}
                            onClick={() =>
                              void change(
                                () =>
                                  post<Delivery>(
                                    `${url}/slots/${encodeURIComponent(slot.key)}/reuse`,
                                    { document_id: slot.reusable_document_id },
                                  ),
                                "Combined permit linked to both requirements.",
                                slot,
                              )
                            }
                          >
                            Confirm the existing permit covers both documents
                          </button>
                        )}
                      {staffEdit &&
                        slot.condition &&
                        !saved &&
                        !waived &&
                        (waiveKey === slot.key ? (
                          <form
                            className={styles.custom}
                            onSubmit={(event) => {
                              event.preventDefault();
                              if (validStaffText(waiver, 500))
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
                            <label>
                              Reason this requirement does not apply
                              <input
                                required
                                disabled={working}
                                value={waiver}
                                onChange={(event) =>
                                  setWaiver(event.target.value)
                                }
                              />
                            </label>
                            <button
                              className={actionStyle}
                              disabled={working || !validStaffText(waiver, 500)}
                            >
                              Save reason
                            </button>
                          </form>
                        ) : (
                          <button
                            type="button"
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
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          {(data.bonus_status === "no_bonus" ||
            (data.bonus_rate === 0 && !data.bonus_status)) && (
            <p className={styles.hint}>This service carries no bonus.</p>
          )}
          {legacyOpen && data.bonus_instruction && (
            <p className={styles.hint}>
              {bonusInstruction(data, data.bonus_instruction)}
            </p>
          )}
          {data.bonus_status === "forfeited" && (
            <p className={styles.hint}>
              No bonus: the client paid less than the service price.
            </p>
          )}
          {writable && !practice.family_member_id && (
            <details className={styles.company}>
              <summary>
                Company filing (optional){chooseCompany && " · enabled"}
                <ChevronDown size={14} aria-hidden />
              </summary>
              <label>
                <input
                  type="checkbox"
                  checked={chooseCompany}
                  disabled={working}
                  onChange={(event) => {
                    setChooseCompany(event.target.checked);
                    setCompanyId("");
                  }}
                />
                Save uploads under a linked company
              </label>
              {chooseCompany && (
                <label>
                  Company for these documents
                  <select
                    value={companyId}
                    disabled={working}
                    onChange={(event) => setCompanyId(event.target.value)}
                  >
                    <option value="">Select a company</option>
                    {companies?.map((company) => (
                      <option
                        key={company.company_id}
                        value={String(company.company_id)}
                      >
                        {company.company_name}
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
            <footer className={styles.footer}>
              <p className={styles.hint}>
                {working
                  ? "Saving changes…"
                  : canDeliver
                    ? "Ready to complete"
                    : data.needs_titles
                      ? "Name the required documents first."
                      : missingSlots.length
                        ? `${missingSlots.length} document${missingSlots.length === 1 ? "" : "s"} remaining`
                        : "Define the required documents first."}
              </p>
              <button
                className={styles.completeButton}
                disabled={working || !canDeliver}
                onClick={() =>
                  void change(
                    () => post<Delivery>(`${url}/complete`),
                    "Final documents delivered. Practice completed.",
                  )
                }
              >
                Complete practice
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
    </section>
  );
}

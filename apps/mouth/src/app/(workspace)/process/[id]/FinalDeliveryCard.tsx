"use client";

import { useRef, useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import type { Practice } from "@/lib/api/crm/crm.types";
import { fileToBase64 } from "@/lib/utils";
import {
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

interface Slot {
  key: string;
  label: string;
  condition: string | null;
  document_id: number | null;
  file_name: string | null;
  waiver_reason: string | null;
  reusable_document_id: number | null;
  issued_on: string | null;
}

interface Delivery {
  status: string;
  bonus_slot: string | null;
  bonus_event:
    | "electronic_visa_issued"
    | "kitas_imk_uploaded"
    | "service_delivered"
    | null;
  visa_followup?: VisaFollowup | null;
  needs_titles: boolean;
  slots: Slot[];
}

const cardStyle =
  "mb-6 rounded-xl border border-[var(--bz-border)] bg-[var(--bz-card)] p-5";
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
    data?.status === "on_process" && practice.payment_status === "paid";
  const visaDateSlot = data?.bonus_slot === "electronic_visa";
  const bonusLabel = visaDateSlot ? "Electronic Visa" : "renewed IMK";
  const canDeliver =
    writable &&
    !data.needs_titles &&
    data.slots.length > 0 &&
    data.slots.every(
      (s) => s.document_id != null || Boolean(s.condition && s.waiver_reason),
    );
  const canUpload =
    !chooseCompany ||
    companies?.some((company) => String(company.company_id) === companyId);

  async function change(request: () => Promise<Delivery>, message: string) {
    if (pending.current || !writable) return;
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
      if (result.status === "completed") onComplete();
    } catch (e) {
      setFailure(
        e instanceof Error
          ? e.message
          : "The change could not be saved. Please retry.",
      );
      // A response can be lost after the server commits. Recover its actual state.
      const refreshed = await mutate().catch(() => undefined);
      if (refreshed?.status === "completed") {
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
    void change(
      async () =>
        post<Delivery>(`${url}/slots/${encodeURIComponent(slot.key)}`, {
          file_name: file.name,
          ...(needsIssueDate(slot) ? { issued_on: issueDate } : {}),
          file_content: await fileToBase64(file),
          mime_type: file.type,
          ...(chooseCompany && companyId
            ? { company_id: Number(companyId) }
            : {}),
        }),
      slot.key === data?.bonus_slot
        ? `${visaDateSlot ? "Electronic Visa" : "Renewed IMK"} upload recorded.`
        : "Document saved.",
    );
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
    );
  }

  return (
    <section
      id="final-delivery"
      className={cardStyle}
      aria-label="Final delivery"
    >
      <h2 className="text-xl font-semibold mb-3">Final delivery</h2>
      {error && (
        <p role="alert">
          Unable to load delivery requirements.{" "}
          <button className={actionStyle} onClick={() => void mutate()}>
            Retry
          </button>
        </p>
      )}
      {!data && !error && <p>Loading delivery requirements…</p>}
      {data && !error && (
        <>
          {data.bonus_slot && (
            <p className="mb-3 text-sm" role="status">
              {data.bonus_event
                ? `Bonus recorded on the ${bonusLabel} upload. Remaining documents do not earn another bonus.`
                : `The bonus accrues when you upload the ${bonusLabel}.${visaDateSlot ? " Enter the printed issue date for the document record." : ""}`}
            </p>
          )}
          {writable && !practice.family_member_id && (
            <div className="mb-3 text-sm">
              <label>
                <input
                  type="checkbox"
                  checked={chooseCompany}
                  disabled={working}
                  onChange={(e) => {
                    setChooseCompany(e.target.checked);
                    setCompanyId("");
                  }}
                />{" "}
                File this upload under a linked company
              </label>
              {chooseCompany && (
                <label className="block mt-1">
                  Company whose document is being delivered
                  <select
                    className="block border rounded p-1"
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
            </div>
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
            <ul className="space-y-2">
              {data.slots.map((slot) => (
                <li
                  key={slot.key}
                  className="rounded-lg border border-[var(--bz-border)] p-3"
                >
                  <div className="font-semibold">{slot.label}</div>
                  {slot.condition && (
                    <p className="text-sm text-[var(--bz-text-2)]">
                      {slot.condition}
                    </p>
                  )}
                  <p className="text-sm break-words">
                    {slot.file_name ||
                      (slot.waiver_reason
                        ? `Not applicable — ${slot.waiver_reason}`
                        : "Awaiting file")}
                  </p>
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
                        <time dateTime={slot.issued_on}>{slot.issued_on}</time>
                      </p>
                    )}
                  {writable && needsIssueDate(slot) && (
                    <label className="block my-1 text-sm">
                      Electronic Visa issue date
                      <input
                        type="date"
                        required
                        max={today}
                        value={issueDate}
                        disabled={working}
                        onChange={(e) => setIssueDate(e.currentTarget.value)}
                        className="block rounded border p-1"
                      />
                    </label>
                  )}
                  {writable && (
                    <>
                      <label className="block text-sm my-1">
                        {slot.document_id == null ? "Upload" : "Replace"}{" "}
                        {slot.label}
                        <input
                          className="block max-w-full text-sm"
                          type="file"
                          accept={UPLOAD_ACCEPT}
                          disabled={
                            working ||
                            !canUpload ||
                            (needsIssueDate(slot) && !validIssueDate)
                          }
                          onChange={(e) => {
                            const f = e.currentTarget.files?.[0];
                            e.currentTarget.value = "";
                            if (f) attach(slot, f);
                          }}
                        />
                      </label>
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
                                    { document_id: slot.reusable_document_id },
                                  ),
                                "Combined permit linked to both requirements.",
                              )
                            }
                          >
                            Confirm the existing permit covers both documents
                          </button>
                        )}
                      {slot.condition &&
                        !slot.document_id &&
                        !slot.waiver_reason &&
                        (waiveKey === slot.key ? (
                          <form
                            onSubmit={(e) => {
                              e.preventDefault();
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
                              disabled={working || !waiver.trim()}
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
          {writable && (
            <button
              className="mt-4 rounded-lg bg-[var(--bz-accent)] px-4 py-2 font-semibold text-[var(--bz-base)] disabled:opacity-50"
              disabled={working || !canDeliver}
              onClick={() =>
                void change(
                  () => post<Delivery>(`${url}/complete`),
                  "Final documents delivered. Practice completed.",
                )
              }
            >
              Deliver and complete
            </button>
          )}
        </>
      )}
      {feedback && (
        <p role="status" className="mt-2 text-sm">
          {feedback}
        </p>
      )}
      {failure && (
        <p role="alert" className="mt-2 text-sm text-[var(--state-danger)]">
          {failure}
        </p>
      )}
    </section>
  );
}

import type { CSSProperties, MouseEvent, ReactNode } from "react";
import {
  ArrowUpRight,
  Check,
  CircleDollarSign,
  Ellipsis,
  FileStack,
  Flag,
  Loader2,
  ReceiptText,
  UserRound,
} from "lucide-react";
import { Money } from "@balizero/core";
import type { Practice } from "@/lib/api/crm/crm.types";
import { WORK_DAY_TIME_ZONE, workDayOf } from "@/lib/format/work-day";
import { initialsOf } from "@/lib/team-initials";
import {
  assigneeLabel,
  departmentLabel,
  processAppearance,
} from "./process-appearance";
import {
  COLUMN_COLORS,
  getStatusColumn,
  type CaseStatus,
} from "./kanban-colors";
import styles from "./ProcessTile.module.css";

export function AssigneeMark({ practice }: { practice: Practice }) {
  const department = departmentLabel(practice);
  return (
    <span
      aria-label={department}
      role="img"
      title={department}
      className={styles.mark}
      data-department={department.toLowerCase()}
      style={processAppearance(practice) as CSSProperties}
    />
  );
}

type ProcessTileProps = {
  practice: Practice;
  updating: boolean;
  openProcess: () => void;
  openClient: () => void;
  openMenu: (event: MouseEvent, practice: Practice) => void;
  onContextMenu: (event: MouseEvent, practice: Practice) => void;
  deliveryPeek?: ReactNode;
};

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: WORK_DAY_TIME_ZONE,
  day: "numeric",
  month: "short",
  year: "numeric",
});

function displayDate(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return <time dateTime={value}>{dateFormatter.format(date)}</time>;
}

function daysToExpiry(value: string | null | undefined) {
  if (!value) return null;
  const due = new Date(value);
  if (!Number.isFinite(due.getTime())) return null;
  return (Date.parse(workDayOf(due)) - Date.parse(workDayOf())) / 86_400_000;
}

function amountOf(value: number | string | null | undefined) {
  if (value == null || (typeof value === "string" && !value.trim()))
    return null;
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? amount : null;
}

function caseFact(practice: Practice, stage: CaseStatus, dueIn: number | null) {
  const done = stage === "completed" && displayDate(practice.completion_date);
  if (done) return { label: "Completed", value: done };
  if (dueIn !== null && dueIn <= 30) {
    return {
      label: dueIn < 0 ? "Expired" : "Expires",
      value: displayDate(practice.expiry_date),
    };
  }
  const received = amountOf(practice.paid_amount);
  if (received !== null && received > 0)
    return { label: "Received", value: <Money value={received} compact /> };
  const fee = amountOf(practice.actual_price);
  if (fee !== null)
    return { label: "Service fee", value: <Money value={fee} compact /> };
  const quote = amountOf(practice.quoted_price);
  if (quote !== null)
    return { label: "Quoted", value: <Money value={quote} compact /> };
  const start = displayDate(practice.start_date);
  if (start) return { label: "Started", value: start };
  const created = displayDate(practice.created_at);
  return created ? { label: "Opened", value: created } : null;
}

const paymentLabels: Record<string, string> = {
  paid: "Paid",
  partial: "Part paid",
  unpaid: "Unpaid",
};

export function ProcessTile({
  practice,
  updating,
  openClient,
  openProcess,
  openMenu,
  onContextMenu,
  deliveryPeek,
}: ProcessTileProps) {
  const department = departmentLabel(practice);
  const owner = assigneeLabel(practice);
  const service =
    practice.practice_type_name ||
    practice.practice_type_code?.replace(/_/g, " ") ||
    "Process";
  const client = practice.client_name || "Unknown Client";
  const column = getStatusColumn(practice.status);
  const rawStatus =
    practice.status
      ?.replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase()) || "Unknown status";
  const subStatus =
    column === "unknown"
      ? `Needs review · ${rawStatus}`
      : rawStatus !== COLUMN_COLORS[column].label
        ? rawStatus
        : null;
  const urgent = practice.priority === "urgent" || practice.priority === "high";
  const dueIn = daysToExpiry(practice.expiry_date);
  const fact = caseFact(practice, column, dueIn);
  const payment = paymentLabels[practice.payment_status];
  const updated =
    practice.updated_at !== practice.created_at &&
    displayDate(practice.updated_at);
  const Stamp = department === "Tax" ? ReceiptText : FileStack;
  return (
    <article
      className={styles.tile}
      data-testid={`process-card-${practice.id}`}
      aria-busy={updating}
      onContextMenu={(event) => onContextMenu(event, practice)}
      style={processAppearance(practice) as CSSProperties}
    >
      <div
        className={styles.face}
        data-department={department.toLowerCase()}
        data-complete={column === "completed"}
      >
        <button
          type="button"
          disabled={updating}
          className={styles.open}
          aria-label={`Open process ${practice.id}: ${service}`}
          title={`${client} · ${service} · ${COLUMN_COLORS[column].label}`}
          onClick={openProcess}
        />
        <div className={styles.identity}>
          <span className={styles.stamp}>
            <Stamp size={15} aria-hidden />
          </span>
          <span>{department}</span>
          <span className={styles.recordId}>#{practice.id}</span>
          <ArrowUpRight className={styles.openArrow} size={15} aria-hidden />
        </div>
        <div className={styles.subject}>
          <h3 className={styles.service} title={service}>
            {service}
          </h3>
          {practice.client_id ? (
            <button
              className={styles.client}
              type="button"
              onClick={openClient}
              aria-label={`${client}, open client profile`}
              title={client}
            >
              <UserRound size={13} aria-hidden />
              <span>{client}</span>
            </button>
          ) : (
            <span className={styles.client}>{client}</span>
          )}
        </div>
        {fact && (
          <dl className={styles.fact}>
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
          </dl>
        )}
      </div>
      <div className={styles.foot}>
        <div className={styles.ownerRow}>
          <span className={styles.avatar} aria-hidden>
            {owner === "Unassigned" ? (
              <UserRound size={14} />
            ) : (
              initialsOf(owner)
            )}
          </span>
          <div className={styles.ownerInfo}>
            <span className={styles.owner} title={owner}>
              {owner}
            </span>
            {updated && (
              <span className={styles.updated}>Updated {updated}</span>
            )}
          </div>
          <button
            type="button"
            disabled={updating}
            onClick={(event) => openMenu(event, practice)}
            className={styles.menu}
            title="More options"
            aria-label="More options"
          >
            {updating ? (
              <Loader2
                aria-hidden
                size={17}
                className="motion-safe:animate-spin"
              />
            ) : (
              <Ellipsis aria-hidden size={17} />
            )}
          </button>
        </div>
        {(payment ||
          urgent ||
          subStatus ||
          practice.family_member_name ||
          (dueIn !== null && dueIn <= 30)) && (
          <div className={styles.signals}>
            {payment && (
              <span
                className={styles.signal}
                data-state={practice.payment_status}
              >
                {practice.payment_status === "paid" ? (
                  <Check size={12} aria-hidden />
                ) : (
                  <CircleDollarSign size={12} aria-hidden />
                )}
                {payment}
              </span>
            )}
            {urgent && (
              <span className={styles.signal} data-state="urgent">
                <Flag size={12} aria-hidden />{" "}
                {practice.priority === "urgent" ? "Urgent" : "High priority"}
              </span>
            )}
            {subStatus && <span>{subStatus}</span>}
            {dueIn !== null && dueIn <= 30 && (
              <span>
                {dueIn < 0
                  ? `Expired ${-dueIn}d ago`
                  : dueIn === 0
                    ? "Expires today"
                    : `Expires in ${dueIn}d`}
              </span>
            )}
            {practice.family_member_name && (
              <span>For {practice.family_member_name}</span>
            )}
          </div>
        )}
        {deliveryPeek}
      </div>
    </article>
  );
}

import type { CSSProperties, MouseEvent } from "react";
import { Ellipsis, Flag, Loader2 } from "lucide-react";
import type { Practice } from "@/lib/api/crm/crm.types";
import {
  assigneeLabel,
  departmentLabel,
  processAppearance,
} from "./process-appearance";
import { COLUMN_COLORS, getStatusColumn } from "./kanban-colors";
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
};

function expiryNotice(value: string | null | undefined) {
  if (!value) return null;
  const days = Math.ceil((new Date(value).getTime() - Date.now()) / 86_400_000);
  if (days < 0) return `Expired ${Math.abs(days)}d ago`;
  if (days === 0) return "Expires today";
  if (days <= 30) return `Expires in ${days}d`;
  return null;
}

export function ProcessTile({
  practice,
  updating,
  openClient,
  openProcess,
  openMenu,
  onContextMenu,
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
  const expiry = expiryNotice(practice.expiry_date);
  const payment =
    practice.payment_status === "unpaid"
      ? "Unpaid"
      : practice.payment_status === "partial"
        ? "Part paid"
        : null;
  return (
    <article
      className={styles.tile}
      data-testid={`process-card-${practice.id}`}
      aria-busy={updating}
      onContextMenu={(event) => onContextMenu(event, practice)}
      style={processAppearance(practice) as CSSProperties}
    >
      <div className={styles.face} data-department={department.toLowerCase()}>
        <button
          type="button"
          disabled={updating}
          className="absolute inset-0 z-[1] rounded-[inherit] cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--bz-focus-ring)]"
          aria-label={`Open process ${practice.id}: ${service}`}
          title={`${client} · ${service} · ${COLUMN_COLORS[column].label}`}
          onClick={openProcess}
        />
        <span className="text-[11px] font-semibold tracking-wider uppercase tabular-nums">
          {department} · #{practice.id}
        </span>
        <span
          className="line-clamp-2 break-words w-full text-base leading-tight font-semibold tracking-tight"
          title={service}
        >
          {service}
        </span>
        {practice.client_id ? (
          <button
            className="relative z-[2] max-w-full truncate text-sm underline-offset-4 hover:underline focus-visible:underline"
            type="button"
            onClick={openClient}
            aria-label={`${client}, open client profile`}
            title={client}
          >
            {client}
          </button>
        ) : (
          <span className="max-w-full truncate text-sm">{client}</span>
        )}
        {practice.family_member_name && (
          <span
            className="max-w-full truncate text-[11px] font-medium"
            title={practice.family_member_name}
          >
            For {practice.family_member_name}
          </span>
        )}
      </div>
      <div className="flex items-center justify-between gap-2">
        <p className="flex min-w-0 items-center gap-2 text-xs font-semibold text-[var(--bz-text-1)]">
          <AssigneeMark practice={practice} />
          <span className="truncate" title={owner}>
            {owner}
          </span>
        </p>
        <button
          type="button"
          disabled={updating}
          onClick={(event) => openMenu(event, practice)}
          className="grid min-h-11 min-w-11 place-items-center rounded-md text-[var(--bz-text-2)] hover:bg-[var(--bz-card-hover)] focus-visible:outline-2 focus-visible:outline-[var(--bz-focus-ring)]"
          title="More options"
          aria-label="More options"
        >
          {updating ? (
            <Loader2 aria-hidden="true" className="size-4 animate-spin" />
          ) : (
            <Ellipsis aria-hidden="true" className="size-4" />
          )}
        </button>
      </div>
      {(urgent || expiry || payment || subStatus) && (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] leading-snug text-[var(--bz-text-2)]">
          {urgent && (
            <span className="inline-flex items-center gap-1 font-semibold capitalize text-[var(--bz-text-1)]">
              <Flag aria-hidden="true" className="size-3" />
              {practice.priority}
            </span>
          )}
          {expiry && <span>{expiry}</span>}
          {payment && <span>{payment}</span>}
          {subStatus && <span>{subStatus}</span>}
        </p>
      )}
    </article>
  );
}

"use client";

import * as React from "react";
import {
  Info,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Lightbulb,
  Zap,
  BookOpen,
  ExternalLink,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ============================================================================
// Types
// ============================================================================

export type InfoCardVariant =
  | "info"
  | "warning"
  | "success"
  | "error"
  | "tip"
  | "important"
  | "note"
  | "highlight";

type LegacyInfoCardVariant = InfoCardVariant | "danger";

/** A labelled row, as the article composer emits for the Quick Summary box. */
export interface InfoCardLabelledItem {
  label: string;
  value: string;
}

/** A row of the card body: a labelled pair or a plain (inline-bold) string. */
export type InfoCardItem = string | InfoCardLabelledItem;

export interface InfoCardProps {
  /** Variant determines color and icon */
  variant?: LegacyInfoCardVariant;
  /** Custom title (optional, uses default based on variant) */
  title?: string;
  /** Content */
  children?: React.ReactNode;
  /** Rows rendered as a list above the children */
  items?: readonly InfoCardItem[];
  /** Optional icon override */
  icon?: React.ReactNode;
  /** Collapsible */
  collapsible?: boolean;
  /** Default collapsed state */
  defaultCollapsed?: boolean;
  /** Link */
  link?: { label: string; url: string };
  /** Custom class */
  className?: string;
}

// ============================================================================
// Variant styles
// ============================================================================

const variantStyles: Record<
  InfoCardVariant,
  {
    bg: string;
    border: string;
    iconBg: string;
    iconColor: string;
    titleColor: string;
    defaultTitle: string;
    icon: React.ComponentType<{ className?: string }>;
  }
> = {
  info: {
    bg: "bg-blue-500/5",
    border: "border-blue-500/20",
    iconBg: "bg-blue-500/20",
    iconColor: "text-blue-400",
    titleColor: "text-blue-400",
    defaultTitle: "Information",
    icon: Info,
  },
  warning: {
    bg: "bg-amber-500/5",
    border: "border-amber-500/20",
    iconBg: "bg-amber-500/20",
    iconColor: "text-amber-400",
    titleColor: "text-amber-400",
    defaultTitle: "Warning",
    icon: AlertTriangle,
  },
  success: {
    bg: "bg-emerald-500/5",
    border: "border-emerald-500/20",
    iconBg: "bg-emerald-500/20",
    iconColor: "text-emerald-400",
    titleColor: "text-emerald-400",
    defaultTitle: "Success",
    icon: CheckCircle2,
  },
  error: {
    bg: "bg-red-500/5",
    border: "border-red-500/20",
    iconBg: "bg-red-500/20",
    iconColor: "text-red-400",
    titleColor: "text-red-400",
    defaultTitle: "Error",
    icon: XCircle,
  },
  tip: {
    bg: "bg-purple-500/5",
    border: "border-purple-500/20",
    iconBg: "bg-purple-500/20",
    iconColor: "text-purple-400",
    titleColor: "text-purple-400",
    defaultTitle: "Pro Tip",
    icon: Lightbulb,
  },
  important: {
    bg: "bg-rose-500/5",
    border: "border-rose-500/20",
    iconBg: "bg-rose-500/20",
    iconColor: "text-rose-400",
    titleColor: "text-rose-400",
    defaultTitle: "Important",
    icon: Zap,
  },
  note: {
    bg: "bg-white/5",
    border: "border-white/10",
    iconBg: "bg-white/10",
    iconColor: "text-white/60",
    titleColor: "text-white/80",
    defaultTitle: "Note",
    icon: BookOpen,
  },
  highlight: {
    bg: "bg-accent-blue-editorial/10",
    border: "border-accent-blue-editorial/30",
    iconBg: "bg-accent-blue-editorial/20",
    iconColor: "text-accent-blue-editorial",
    titleColor: "text-accent-blue-editorial",
    defaultTitle: "Highlight",
    icon: Zap,
  },
};

// ============================================================================
// Items
// ============================================================================

/** Renders **bold** spans of a plain string row; MDX props are not parsed as markdown. */
function renderInline(text: string): React.ReactNode[] {
  return text.split(/\*\*(.+?)\*\*/g).map((part, i) =>
    i % 2 === 1 ? (
      <strong key={i} className="font-semibold text-white/90">
        {part}
      </strong>
    ) : (
      <React.Fragment key={i}>{part}</React.Fragment>
    ),
  );
}

type NormalizedRow =
  | { kind: "text"; text: string }
  | { kind: "labelled"; label: string; value: string | null };

/**
 * MDX props are unchecked at runtime, so rows are coerced defensively:
 * null/undefined and unusable objects are skipped, non-string scalars render
 * as String(item), and a labelled row without a string value renders the
 * label alone.
 */
function normalizeRow(item: unknown): NormalizedRow | null {
  if (item === null || item === undefined) return null;
  if (typeof item === "string") return { kind: "text", text: item };
  if (
    typeof item === "number" ||
    typeof item === "boolean" ||
    typeof item === "bigint"
  ) {
    return { kind: "text", text: String(item) };
  }
  if (typeof item !== "object") return null;
  const { label, value } = item as { label?: unknown; value?: unknown };
  const labelText = typeof label === "string" && label !== "" ? label : null;
  const valueText = typeof value === "string" && value !== "" ? value : null;
  if (labelText !== null) {
    return { kind: "labelled", label: labelText, value: valueText };
  }
  return valueText !== null ? { kind: "text", text: valueText } : null;
}

function InfoCardItems({
  items,
  labelClassName,
}: {
  items: readonly unknown[];
  labelClassName: string;
}) {
  const rows = items
    .map(normalizeRow)
    .flatMap((row, index) => (row ? [{ row, index }] : []));
  if (rows.length === 0) return null;

  return (
    <ul className="space-y-2 list-none p-0 m-0 mb-2 last:mb-0">
      {rows.map(({ row, index }) =>
        row.kind === "text" ? (
          <li key={index} className="flex gap-2">
            <span aria-hidden="true" className={labelClassName}>
              •
            </span>
            <span>{renderInline(row.text)}</span>
          </li>
        ) : (
          <li key={index}>
            <span className={cn("font-medium", labelClassName)}>
              {row.label}
            </span>
            {row.value !== null && (
              <>
                <span className="text-white/40">{": "}</span>
                <span>{renderInline(row.value)}</span>
              </>
            )}
          </li>
        ),
      )}
    </ul>
  );
}

// ============================================================================
// Main Component
// ============================================================================

export function InfoCard({
  variant = "info",
  title,
  children,
  items,
  icon,
  collapsible = false,
  defaultCollapsed = false,
  link,
  className,
}: InfoCardProps) {
  const [isCollapsed, setIsCollapsed] = React.useState(defaultCollapsed);
  const normalizedVariant = variant === "danger" ? "error" : variant;
  const styles = variantStyles[normalizedVariant] ?? variantStyles.info;
  const Icon = styles.icon;
  const displayTitle = title || styles.defaultTitle;

  return (
    <div
      className={cn(
        "rounded-xl border overflow-hidden",
        styles.bg,
        styles.border,
        className,
      )}
    >
      {/* Header */}
      <div
        className={cn(
          "flex items-center gap-3 px-4 py-3",
          collapsible && "cursor-pointer hover:bg-white/5 transition-colors",
        )}
        onClick={collapsible ? () => setIsCollapsed(!isCollapsed) : undefined}
      >
        <div className={cn("p-1.5 rounded-lg", styles.iconBg)}>
          {icon || <Icon className={cn("w-4 h-4", styles.iconColor)} />}
        </div>
        <span className={cn("font-medium text-sm", styles.titleColor)}>
          {displayTitle}
        </span>
        {collapsible && (
          <svg
            className={cn(
              "w-4 h-4 ml-auto transition-transform text-white/40",
              isCollapsed && "rotate-180",
            )}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M19 9l-7 7-7-7"
            />
          </svg>
        )}
      </div>

      {/* Content */}
      {(!collapsible || !isCollapsed) && (
        <div className="px-4 pb-4">
          <div className="text-sm text-white/70 leading-relaxed">
            {Array.isArray(items) && items.length > 0 && (
              <InfoCardItems items={items} labelClassName={styles.titleColor} />
            )}
            {children}
          </div>

          {/* Link */}
          {link && (
            <a
              href={link.url}
              className={cn(
                "inline-flex items-center gap-1.5 mt-3 text-sm",
                styles.titleColor,
                "hover:underline",
              )}
            >
              <span>{link.label}</span>
              <ExternalLink className="w-3 h-3" />
            </a>
          )}
        </div>
      )}
    </div>
  );
}

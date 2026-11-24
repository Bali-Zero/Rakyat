"use client";

import React, {
  useState,
  useEffect,
  useRef,
  useMemo,
  useCallback,
} from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import {
  FilterBar,
  FilterSelect,
  ListPageHeader,
  Money,
  SearchBox,
} from "@balizero/core";
import {
  FolderKanban,
  Filter,
  Plus,
  LayoutGrid,
  List,
  ChevronDown,
  MessageCircle,
  Mail,
  FileText,
  MoreVertical,
  CheckCircle2,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Download,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import {
  QUOTED_PRICE_REQUIRED_TEXT_ID,
  invoiceAmountRefusalText,
  managedStageReason,
  needsQuotedPriceBefore,
} from "@/lib/api/crm/state-machine";
import { api } from "@/lib/api";
import { logger } from "@/lib/logger";
import { toError } from "@/lib/types/common";
import type { Practice } from "@/lib/api/crm/crm.types";
import {
  isSameIdentity,
  readSessionIdentity,
  useSessionIdentity,
  type SessionIdentity,
} from "@/lib/auth/session-identity";
import { ApiError } from "@/lib/api/error-handler";
import { ProcessTile, AssigneeMark } from "@/components/process/ProcessTile";
import { assigneeLabel } from "@/components/process/process-appearance";
import { MonthPillTabs } from "@/components/process/MonthPillTabs";
import { GhostCard } from "@/components/process/GhostCard";
import {
  COLUMN_COLORS,
  COLUMN_ORDER,
  getStatusColumn,
  matchesStatusFilter,
  type CaseStatus,
} from "@/components/process/kanban-colors";
import {
  trackViewModeChange,
  trackFilterApplied,
  trackFilterRemoved,
  trackSortApplied,
  trackSearch,
  trackCaseStatusChanged,
  trackPaginationChange,
  initializeAnalytics,
} from "@/lib/analytics";
import { useDebounce } from "@/hooks/useDebounce";
import {
  PROCESS_VIEW_MODE_KEY,
  loadViewMode,
  saveViewMode,
} from "@/lib/utils/view-mode-storage";

// SIMPLIFIED WORKFLOW (Feb 2026) - 5 Steps:
// inquiry → waiting_documents → sending_invoice → on_process → completed
//
// NOTE: waiting_payment, submitted_to_gov, approved are now internal substates
// handled within the main workflow steps
//
// LEGACY STATUS (for backward compatibility):
// quotation_sent, payment_pending, waiting_payment, in_progress, submitted_to_gov, approved

// Backend status values mapped to frontend display names and kanban columns
// Simplified 5-step workflow
const STATUS_OPTIONS: { value: string; label: string; column: CaseStatus }[] = [
  // Main workflow states (5 steps)
  { value: "inquiry", label: "Inquiry", column: "inquiry" },
  {
    value: "waiting_documents",
    label: "Waiting Documents",
    column: "waiting_documents",
  },
  {
    value: "sending_invoice",
    label: "Sending Invoice",
    column: "sending_invoice",
  },
  { value: "on_process", label: "On Process", column: "on_process" },
  { value: "completed", label: "Completed", column: "completed" },
  // Legacy states mapped to simplified workflow
  {
    value: "quotation_sent",
    label: "Quotation Sent (Legacy)",
    column: "sending_invoice",
  },
  {
    value: "payment_pending",
    label: "Payment Pending (Legacy)",
    column: "sending_invoice",
  },
  {
    value: "waiting_payment",
    label: "Waiting Payment (Legacy)",
    column: "sending_invoice",
  },
  {
    value: "in_progress",
    label: "In Progress (Legacy)",
    column: "on_process",
  },
  {
    value: "submitted_to_gov",
    label: "Submitted to Gov (Legacy)",
    column: "on_process",
  },
  {
    value: "approved",
    label: "Approved (Legacy)",
    column: "on_process",
  },
];

type ViewMode = "kanban" | "list";
type SortField =
  | "id"
  | "practice_type_code"
  | "client_name"
  | "assigned_to"
  | "status"
  | "priority"
  | "created_at"
  | "updated_at";
type SortOrder = "asc" | "desc";

// Practice type catalog — mirrors GET /api/crm/practices/types/catalog
// (same shape consumed by process/new/page.tsx and SelectServiceCard.tsx).
interface CatalogService {
  code: string;
  name: string;
}

interface CatalogCategory {
  code: string;
  label: string;
  services: CatalogService[];
}

interface FilterState {
  status: string;
  type: string;
  assigned_to: string;
  payment_filter: string;
  priority: string;
  expiring: boolean;
  stale: boolean;
}

const FILTER_SELECT_CLASS =
  "border border-[var(--bz-border)] bg-[var(--bz-base)] text-[var(--bz-text-1)] focus:ring-2 focus:ring-[var(--bz-accent)]/50";

// Status dot per kanban column — shared by list-view table + context menu.
// WS2: reads the canonical column palette (kanban-colors) instead of its own
// hardcoded Tailwind step map, so board, table and menu can never drift apart.
const statusDotStyle = (column: CaseStatus): React.CSSProperties => ({
  backgroundColor: COLUMN_COLORS[column].textColor,
});

function validatedMonth(requested: string | null, current: string) {
  return requested &&
    /^\d{4}-(0[1-9]|1[0-2])$/.test(requested) &&
    requested <= current
    ? requested
    : current;
}

/** WhatsApp + Email quick actions — identical on kanban cards and table rows. */
function ContactQuickActions({ practice }: { practice: Practice }) {
  return (
    <>
      {practice.client_phone && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            const phone = practice.client_phone?.replace(/\D/g, "");
            window.open(
              `https://wa.me/${phone}?text=Hi ${practice.client_name}, regarding your process...`,
              "_blank",
            );
          }}
          className="p-1.5 rounded touch-manipulation hover:bg-[color-mix(in_srgb,var(--accent-whatsapp)_20%,transparent)] text-[var(--accent-whatsapp)] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-whatsapp)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bz-card)]"
          title="WhatsApp"
          aria-label="Contact via WhatsApp"
        >
          <MessageCircle className="w-3.5 h-3.5" />
        </button>
      )}
      {practice.client_email && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            window.open(`mailto:${practice.client_email}`, "_blank");
          }}
          className="p-1.5 rounded touch-manipulation hover:bg-[color-mix(in_srgb,var(--state-info)_20%,transparent)] text-[var(--state-info)] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--state-info)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bz-card)]"
          title="Email"
          aria-label="Send email"
        >
          <Mail className="w-3.5 h-3.5" />
        </button>
      )}
    </>
  );
}

export default function PratichePage() {
  const identity = useSessionIdentity();
  if (!identity)
    return (
      <p role="status" className="text-sm text-[var(--bz-text-2)]">
        Sign in to view your processes.
      </p>
    );
  return (
    <ProcessBoard
      key={JSON.stringify([identity.sub, identity.epoch])}
      identity={identity}
    />
  );
}

function ProcessBoard({ identity }: { identity: SessionIdentity }) {
  const router = useRouter();
  const toast = useToast();
  const [practices, setPractices] = useState<Practice[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");
  // The backend folds case, accents, word order and phone formats; the list on screen was loaded
  // for `loadedSearch`. The local text filter only answers while that differs from the typed text.
  const debouncedSearch = useDebounce(searchQuery.trim(), 300);
  const [loadedSearch, setLoadedSearch] = useState("");
  const loadedMonthRef = useRef<string | null>(null);
  // P2.2: persist the view toggle across navigations (lazy-init + write-through)
  const [viewMode, setViewMode] = useState<ViewMode>(() =>
    loadViewMode(PROCESS_VIEW_MODE_KEY, ["kanban", "list"], "kanban"),
  );
  const [showFilters, setShowFilters] = useState(false);
  const [filters, setFilters] = useState<FilterState>({
    status: "",
    type: "",
    assigned_to: "",
    payment_filter: "",
    priority: "",
    expiring: false,
    stale: false,
  });
  const [sortField, setSortField] = useState<SortField>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [loadError, setLoadError] = useState(false);
  const [reload, setReload] = useState(0);
  // Process Type filter options — live catalog, replacing the migration-066
  // legacy codes that used to empty the list on selection (kita-prune lot 6).
  const [typeCatalog, setTypeCatalog] = useState<CatalogCategory[]>([]);

  // Context Menu State
  const [selectedPractice, setSelectedPractice] = useState<Practice | null>(
    null,
  );
  const [menuPosition, setMenuPosition] = useState<{
    x: number;
    y: number;
  } | null>(null);
  const [updatingId, setUpdatingId] = useState<number | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const previousFiltersRef = useRef<FilterState>(filters);

  // Pagination for large datasets
  const [listPageNumber, setListPageNumber] = useState(1);
  const itemsPerPage = 25;

  // Month navigation
  const searchParams = useSearchParams();
  const pathname = usePathname();

  const now = new Date();
  const currentMonthDefault = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const [selectedMonth, setSelectedMonth] = useState(
    validatedMonth(searchParams.get("month"), currentMonthDefault),
  );
  const [completedCollapsed, setCompletedCollapsed] = useState(true);
  const [expandedGhosts, setExpandedGhosts] = useState<Record<string, boolean>>(
    {},
  );

  const handleMonthChange = useCallback(
    (month: string) => {
      setSelectedMonth(month);
      setListPageNumber(1);
      setExpandedGhosts({});
      const params = new URLSearchParams(searchParams.toString());
      params.set("month", month);
      window.history.replaceState(null, "", `${pathname}?${params.toString()}`);
    },
    [searchParams, pathname],
  );

  useEffect(() => {
    // Initialize analytics on component mount
    initializeAnalytics();
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadPractices = async () => {
      // A new month shows the skeleton; a new search text refines the list in place.
      if (loadedMonthRef.current !== selectedMonth) setIsLoading(true);
      try {
        setLoadError(false);
        const rows = new Map<number, Practice>();
        const limit = 200;
        let offset = 0;
        for (;;) {
          const page = await api.crm.getPractices({
            limit,
            offset,
            month: selectedMonth,
            include_history: true,
            session: identity,
            ...(debouncedSearch ? { search: debouncedSearch } : {}),
          });
          if (cancelled || !isSameIdentity(readSessionIdentity(), identity))
            return;
          for (const practice of page) rows.set(practice.id, practice);
          if (page.length < limit) break;
          offset += page.length;
        }
        loadedMonthRef.current = selectedMonth;
        setPractices([...rows.values()]);
        setLoadedSearch(debouncedSearch);
      } catch (error) {
        if (cancelled || !isSameIdentity(readSessionIdentity(), identity))
          return;
        if (
          loadedMonthRef.current !== selectedMonth ||
          (error instanceof ApiError && [401, 403].includes(error.statusCode))
        ) {
          setPractices([]);
          setSelectedPractice(null);
        }
        setLoadError(true);
        logger.error(
          "Failed to load practices",
          { component: "Process", action: "loadPractices" },
          toError(error),
        );
        toast.error("Error", "Failed to load process");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    loadPractices();
    return () => {
      cancelled = true;
    };
  }, [selectedMonth, debouncedSearch, identity, reload]);

  useEffect(() => {
    // Live "Process Type" filter options — same catalog endpoint the
    // process-creation form uses, so the dropdown never lists a code the
    // state machine has retired.
    api.crm
      .getPracticeTypesCatalog()
      .then((data) => setTypeCatalog(data.categories))
      .catch((err: unknown) => {
        logger.error(
          "[Process] Failed to load practice types catalog",
          { component: "Process", action: "loadTypeCatalog" },
          toError(err),
        );
      });
  }, []);

  // Track + persist view mode changes
  useEffect(() => {
    trackViewModeChange(viewMode);
    saveViewMode(PROCESS_VIEW_MODE_KEY, viewMode);
  }, [viewMode]);

  // Track filter changes
  useEffect(() => {
    Object.keys(filters).forEach((key) => {
      if (
        filters[key as keyof typeof filters] !==
        previousFiltersRef.current[key as keyof typeof filters]
      ) {
        if (filters[key as keyof typeof filters]) {
          if (key === "status" || key === "type" || key === "assigned_to") {
            const val = filters[key as keyof typeof filters];
            if (typeof val === "string")
              trackFilterApplied(key, key === "assigned_to" ? "selected" : val);
          }
        } else {
          trackFilterRemoved(key);
        }
      }
    });
    previousFiltersRef.current = filters;
  }, [filters]);

  // Track sort changes
  useEffect(() => {
    trackSortApplied(sortField, sortOrder);
  }, [sortField, sortOrder]);

  // Track pagination changes
  useEffect(() => {
    if (listPageNumber > 1) {
      trackPaginationChange(listPageNumber, itemsPerPage);
    }
  }, [listPageNumber]);

  // Close menu when clicking outside or pressing Escape
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setSelectedPractice(null);
        setMenuPosition(null);
      }
    };
    const handleEscapeMenu = (e: KeyboardEvent) => {
      if (e.key === "Escape" && menuPosition) {
        setSelectedPractice(null);
        setMenuPosition(null);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleEscapeMenu);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleEscapeMenu);
    };
  }, [menuPosition]);

  const handleStatusChange = async (practiceId: number, newStatus: string) => {
    if (
      managedStageReason(newStatus) ||
      !selectedPractice?.can_edit_process ||
      !isSameIdentity(readSessionIdentity(), identity)
    )
      return;
    setUpdatingId(practiceId);
    try {
      // Find old status for tracking
      const practice = practices.find((p) => p.id === practiceId);
      const oldStatus = practice?.status || "unknown";

      await api.crm.updatePractice(practiceId, { status: newStatus });

      // Update local state immediately for responsiveness
      setPractices((prev) =>
        prev.map((p) =>
          p.id === practiceId ? { ...p, status: newStatus } : p,
        ),
      );

      // Track status change
      trackCaseStatusChanged(practiceId, oldStatus, newStatus);

      toast.success(
        "Status Updated",
        `Process moved to ${newStatus.replace(/_/g, " ")}`,
      );

      setSelectedPractice(null);
      setMenuPosition(null);
    } catch (error) {
      logger.error(
        "Failed to update status",
        { component: "Process", action: "updateStatus" },
        toError(error),
      );
      toast.error(
        "Error",
        invoiceAmountRefusalText(error) ?? "Failed to update process status",
      );
    } finally {
      setUpdatingId(null);
    }
  };

  const handleMenuClick = (e: React.MouseEvent, practice: Practice) => {
    e.preventDefault();
    e.stopPropagation();

    // Position menu near the click
    const rect = e.currentTarget.getBoundingClientRect();
    setSelectedPractice(practice);
    setMenuPosition({ x: rect.right - 10, y: rect.bottom + 5 });
  };

  const handleContextMenu = (e: React.MouseEvent, practice: Practice) => {
    e.preventDefault();
    e.stopPropagation();
    setSelectedPractice(practice);
    setMenuPosition({ x: e.clientX, y: e.clientY });
  };

  const handlePriorityChange = async (
    practiceId: number,
    newPriority: string,
  ) => {
    if (
      !selectedPractice?.can_edit_process ||
      !isSameIdentity(readSessionIdentity(), identity)
    )
      return;
    setUpdatingId(practiceId);
    try {
      await api.crm.updatePractice(practiceId, { priority: newPriority });
      setPractices((prev) =>
        prev.map((p) =>
          p.id === practiceId ? { ...p, priority: newPriority } : p,
        ),
      );
      setSelectedPractice((prev) =>
        prev?.id === practiceId ? { ...prev, priority: newPriority } : prev,
      );
      toast.success("Priority Updated", `→ ${newPriority}`);
    } catch (error) {
      logger.error(
        "Failed to update priority",
        { component: "Process", action: "updatePriority" },
        toError(error),
      );
      toast.error("Error", "Failed to update priority");
    } finally {
      setUpdatingId(null);
    }
  };

  const handleNewCase = () => {
    router.push("/process/new");
  };

  const clearFilters = () => {
    setFilters({
      status: "",
      type: "",
      assigned_to: "",
      payment_filter: "",
      priority: "",
      expiring: false,
      stale: false,
    });
  };

  const toggleSort = useCallback((field: SortField) => {
    setSortField((prevField) => {
      if (prevField === field) {
        setSortOrder((prevOrder) => (prevOrder === "asc" ? "desc" : "asc"));
        return prevField;
      } else {
        setSortOrder("asc");
        return field;
      }
    });
  }, []);

  useEffect(() => {
    setListPageNumber(1);
  }, [filters, debouncedSearch]);

  const activeFiltersCount = Object.values(filters).filter(Boolean).length;

  // Memoize filtered and sorted practices to avoid unnecessary recalculations
  const filteredPractices = useMemo(
    () =>
      practices
        .filter((p) => {
          // Search filter
          if (searchQuery && searchQuery.trim() !== loadedSearch) {
            const matchesSearch =
              p.id.toString().includes(searchQuery) ||
              p.client_name
                ?.toLowerCase()
                .includes(searchQuery.toLowerCase()) ||
              p.practice_type_code
                ?.toLowerCase()
                .includes(searchQuery.toLowerCase());
            if (!matchesSearch) return false;
          }

          // Status filter
          if (
            filters.status &&
            !matchesStatusFilter(p.status, filters.status)
          ) {
            return false;
          }

          // Type filter
          if (filters.type && p.practice_type_code !== filters.type) {
            return false;
          }

          // Assigned to filter
          if (
            filters.assigned_to &&
            p.assigned_to?.trim().toLowerCase() !== filters.assigned_to
          ) {
            return false;
          }

          if (filters.payment_filter === "paid" && p.payment_status !== "paid")
            return false;

          // Payment filter
          if (filters.payment_filter === "unpaid") {
            if (
              p.payment_status !== "unpaid" &&
              p.payment_status !== "partial"
            ) {
              return false;
            }
          }

          // Priority filter
          if (filters.priority && p.priority !== filters.priority) {
            return false;
          }

          // Expiring soon filter (within 30 days)
          if (filters.expiring) {
            if (!p.expiry_date) return false;
            const daysLeft = Math.ceil(
              (new Date(p.expiry_date).getTime() - Date.now()) / 86400000,
            );
            if (daysLeft < 0 || daysLeft > 30) return false;
          }

          // Stale filter: not updated in >14 days (only non-completed)
          if (filters.stale) {
            if (getStatusColumn(p.status) === "completed") return false;
            if (!p.updated_at) return true;
            const ageDays = Math.floor(
              (Date.now() - new Date(p.updated_at).getTime()) / 86400000,
            );
            if (ageDays <= 14) return false;
          }

          return true;
        })
        .sort((a, b) => {
          let comparison = 0;

          switch (sortField) {
            case "id":
              comparison = (a.id || 0) - (b.id || 0);
              break;
            case "practice_type_code":
              comparison = (a.practice_type_code || "").localeCompare(
                b.practice_type_code || "",
              );
              break;
            case "client_name":
              comparison = (a.client_name || "").localeCompare(
                b.client_name || "",
              );
              break;
            case "assigned_to":
              comparison = assigneeLabel(a).localeCompare(assigneeLabel(b));
              break;
            case "status":
              comparison = (a.status || "").localeCompare(b.status || "");
              break;
            case "priority": {
              const priorityOrder: Record<string, number> = {
                urgent: 0,
                high: 1,
                normal: 2,
              };
              comparison =
                (priorityOrder[a.priority || "normal"] ?? 2) -
                (priorityOrder[b.priority || "normal"] ?? 2);
              break;
            }
            case "created_at":
              comparison =
                new Date(a.created_at || 0).getTime() -
                new Date(b.created_at || 0).getTime();
              break;
            case "updated_at":
              comparison =
                new Date(a.updated_at || 0).getTime() -
                new Date(b.updated_at || 0).getTime();
              break;
            default:
              comparison = 0;
          }

          return sortOrder === "asc" ? comparison : -comparison;
        }),
    [practices, searchQuery, loadedSearch, filters, sortField, sortOrder],
  );

  const visibleAssignees = useMemo(() => {
    const options = new Map<string, string>();
    for (const practice of practices) {
      const key = practice.assigned_to?.trim().toLowerCase();
      if (key) options.set(key, assigneeLabel(practice));
    }
    return [...options].sort(([, a], [, b]) => a.localeCompare(b));
  }, [practices]);

  // Memoize practices by status to avoid unnecessary recalculations
  // Simplified 5-step workflow: inquiry → waiting_documents → sending_invoice → on_process → completed
  const practicesByStatus = useMemo(
    () => ({
      inquiry: filteredPractices.filter((p) => {
        const statusColumn = getStatusColumn(p.status);
        return statusColumn === "inquiry" || statusColumn === "unknown";
      }),
      waiting_documents: filteredPractices.filter(
        (p) => getStatusColumn(p.status) === "waiting_documents",
      ),
      sending_invoice: filteredPractices.filter(
        (p) => getStatusColumn(p.status) === "sending_invoice",
      ),
      on_process: filteredPractices.filter(
        (p) => getStatusColumn(p.status) === "on_process",
      ),
      completed: filteredPractices.filter(
        (p) => getStatusColumn(p.status) === "completed",
      ),
    }),
    [filteredPractices],
  );

  // Track search operations (moved after filteredPractices declaration)
  useEffect(() => {
    if (searchQuery) {
      trackSearch(searchQuery, filteredPractices.length);
    }
  }, [searchQuery, filteredPractices.length]);

  const exportCSV = useCallback(() => {
    const rows = filteredPractices.map((p) => ({
      ID: p.id,
      Client: p.client_name || "",
      Type: p.practice_type_code || "",
      Status: p.status || "",
      "Assigned To": assigneeLabel(p),
      "Payment Status": p.payment_status || "",
      "Quoted Price": p.quoted_price || "",
      "Actual Price": p.actual_price || "",
      "Created At": p.created_at
        ? new Date(p.created_at).toLocaleDateString("en-GB")
        : "",
      "Updated At": p.updated_at
        ? new Date(p.updated_at).toLocaleDateString("en-GB")
        : "",
    }));
    const headers = Object.keys(rows[0] || {});
    const csv = [
      headers.join(","),
      ...rows.map((r) =>
        headers
          .map((h) => `"${String(r[h as keyof typeof r]).replace(/"/g, '""')}"`)
          .join(","),
      ),
    ].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `processes_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success("Exported", `${rows.length} processes downloaded`);
  }, [filteredPractices, toast]);

  // Pagination for list view
  const paginatedPractices = useMemo(() => {
    const startIdx = (listPageNumber - 1) * itemsPerPage;
    return filteredPractices.slice(startIdx, startIdx + itemsPerPage);
  }, [filteredPractices, listPageNumber]);

  const totalPages = Math.ceil(filteredPractices.length / itemsPerPage);

  const SkeletonCard = () => (
    <div className="p-3 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-card)] space-y-2">
      <div className="h-4 bg-[var(--bz-surface)] rounded w-3/4 animate-pulse" />
      <div className="h-3 bg-[var(--bz-surface)] rounded w-1/2 animate-pulse" />
      <div className="flex gap-2 mt-4 pt-3 border-t border-[var(--bz-border)]">
        <div className="h-6 w-6 bg-[var(--bz-surface)] rounded animate-pulse" />
        <div className="h-6 w-6 bg-[var(--bz-surface)] rounded animate-pulse" />
      </div>
    </div>
  );

  return (
    <div className="sentry-block min-w-0 space-y-5" data-sentry-block="">
      <ListPageHeader
        variant="desk"
        title="Process"
        subtitle={
          isLoading
            ? "Loading processes…"
            : loadError
              ? undefined
              : `${filteredPractices.length - practicesByStatus.completed.length} active · ${practicesByStatus.completed.length} completed`
        }
        actions={
          <>
            <MonthPillTabs
              selectedMonth={selectedMonth}
              onMonthChange={handleMonthChange}
            />
            <Button
              className="gap-2 bg-[var(--bz-accent)] text-[var(--accent-foreground)] hover:bg-[var(--bz-accent)]/90"
              onClick={handleNewCase}
            >
              <Plus aria-hidden="true" className="w-4 h-4" />
              New Process
            </Button>
          </>
        }
      />

      {/* Search & Filter Bar */}
      <div className="flex flex-col gap-3">
        <div className="flex flex-col sm:flex-row gap-3">
          <SearchBox
            clearable
            value={searchQuery}
            onValueChange={setSearchQuery}
            placeholder="Search process…"
            ariaLabel="Search process"
            title="Press / to focus search, Escape to clear"
            className="border border-[var(--bz-border)] bg-[var(--bz-card)] text-[var(--bz-text-1)] placeholder:text-[var(--bz-text-2)] focus:ring-2 focus:ring-[var(--bz-accent)]/50 transition-all"
          />
          <div className="flex gap-2">
            <Button
              variant={showFilters ? "default" : "outline"}
              className="gap-2 border-[var(--bz-border)] bg-[var(--bz-card)] text-[var(--bz-text-1)] hover:bg-[var(--bz-card-hover)]"
              aria-expanded={showFilters}
              onClick={() => setShowFilters(!showFilters)}
            >
              <Filter className="w-4 h-4" />
              Filters
              {activeFiltersCount > 0 && (
                <span className="ml-1 rounded-full bg-[var(--bz-accent)] px-1.5 py-0.5 text-xs text-[var(--accent-foreground)]">
                  {activeFiltersCount}
                </span>
              )}
            </Button>
            <Button
              variant="outline"
              className="gap-2 border-[var(--bz-border)] bg-[var(--bz-card)] text-[var(--bz-text-2)] hover:bg-[var(--bz-card-hover)] hover:text-[var(--bz-text-1)]"
              onClick={exportCSV}
              title={`Export ${filteredPractices.length} processes as CSV`}
              aria-label={`Export ${filteredPractices.length} processes as CSV`}
            >
              <Download className="w-4 h-4" />
              <span className="hidden sm:inline">Export</span>
            </Button>
            <div className="flex rounded-lg border border-[var(--bz-border)] bg-[var(--bz-card)] overflow-hidden">
              {(
                [
                  { mode: "kanban", icon: LayoutGrid, label: "Kanban Board" },
                  { mode: "list", icon: List, label: "List View" },
                ] as const
              ).map(({ mode, icon: Icon, label }) => (
                <button
                  key={mode}
                  onClick={() => setViewMode(mode)}
                  className={`grid min-h-11 min-w-11 place-items-center transition-colors ${
                    viewMode === mode
                      ? "bg-[var(--bz-accent)]/10 text-[var(--bz-accent)]"
                      : "text-[var(--bz-text-2)] hover:bg-[var(--bz-card)]"
                  }`}
                  title={label}
                  aria-label={label}
                  aria-pressed={viewMode === mode}
                >
                  <Icon className="w-4 h-4" />
                </button>
              ))}
            </div>
          </div>
        </div>

        {showFilters && (
          <FilterBar
            activeCount={activeFiltersCount}
            onClearAll={clearFilters}
            className="bz-product-panel rounded-lg"
            gridClassName="grid grid-cols-1 sm:grid-cols-3 gap-4"
          >
            <FilterSelect
              id="status-filter"
              label="Status"
              value={filters.status}
              onChange={(v) => setFilters({ ...filters, status: v })}
              selectClassName={FILTER_SELECT_CLASS}
            >
              <option value="">All statuses</option>
              <option value="inquiry">Inquiry</option>
              <option value="waiting_documents">Waiting Documents</option>
              <option value="sending_invoice">Sending Invoice</option>
              <option value="on_process">On Process</option>
              <option value="completed">Completed</option>
            </FilterSelect>
            <FilterSelect
              id="type-filter"
              label="Process Type"
              value={filters.type}
              onChange={(v) => setFilters({ ...filters, type: v })}
              selectClassName={FILTER_SELECT_CLASS}
            >
              <option value="">All types</option>
              {typeCatalog.map((cat) => (
                <optgroup key={cat.code} label={cat.label}>
                  {cat.services.map((svc) => (
                    <option key={svc.code} value={svc.code}>
                      {svc.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </FilterSelect>
            <FilterSelect
              id="assigned-to-filter"
              label="Assigned To"
              value={filters.assigned_to}
              onChange={(v) => setFilters({ ...filters, assigned_to: v })}
              selectClassName={FILTER_SELECT_CLASS}
            >
              <option value="">All assignees</option>
              {visibleAssignees.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </FilterSelect>
            <FilterSelect
              id="priority-filter"
              label="Priority"
              value={filters.priority}
              onChange={(v) => setFilters({ ...filters, priority: v })}
              selectClassName={FILTER_SELECT_CLASS}
            >
              <option value="">All priorities</option>
              <option value="urgent">Urgent</option>
              <option value="high">High</option>
              <option value="normal">Normal</option>
            </FilterSelect>
            <FilterSelect
              id="payment-filter"
              label="Payment"
              value={filters.payment_filter}
              onChange={(payment_filter) =>
                setFilters({ ...filters, payment_filter })
              }
              selectClassName={FILTER_SELECT_CLASS}
            >
              <option value="">All payments</option>
              <option value="paid">Paid</option>
              <option value="unpaid">Unpaid or part paid</option>
            </FilterSelect>
            <div className="flex flex-col justify-end gap-1 text-sm text-[var(--bz-text-1)]">
              <label className="flex min-h-8 items-center gap-2">
                <input
                  type="checkbox"
                  checked={filters.expiring}
                  onChange={(e) =>
                    setFilters({ ...filters, expiring: e.target.checked })
                  }
                />
                Expires within 30 days
              </label>
              <label className="flex min-h-8 items-center gap-2">
                <input
                  type="checkbox"
                  checked={filters.stale}
                  onChange={(e) =>
                    setFilters({ ...filters, stale: e.target.checked })
                  }
                />
                No update for 14 days
              </label>
            </div>
          </FilterBar>
        )}
      </div>

      {loadError && (
        <p role="alert" className="text-sm text-[var(--bz-text-2)]">
          Could not refresh this view.{" "}
          <button
            type="button"
            className="underline"
            onClick={() => setReload((value) => value + 1)}
          >
            Try again
          </button>
        </p>
      )}
      {/* Kanban Board View - Simplified 5-Step Workflow */}
      {viewMode === "kanban" && (
        <section aria-label="Workflow stages" className="min-w-0">
          <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--bz-text-2)]">
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-3 border border-current rounded-[2px]"
              />
              Setup
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-3 border border-current rounded-full"
              />
              Tax
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="h-2.5 w-4 border border-current rounded"
              />
              Other
            </span>
            <span className="sm:ml-auto">
              Each assignee’s color deepens with progress.
            </span>
          </div>
          <div
            role="region"
            aria-label="Process board"
            tabIndex={0}
            className="grid min-w-0 grid-flow-col auto-cols-[minmax(13rem,78%)] gap-5 overflow-x-auto overscroll-x-contain snap-x snap-proximity pb-4 sm:auto-cols-[minmax(13rem,1fr)]"
          >
            {COLUMN_ORDER.map((stage) => {
              const column = practicesByStatus[stage];
              const completed = stage === "completed";
              const collapsed = completed && completedCollapsed;
              const history = filteredPractices.filter(
                (row) =>
                  getStatusColumn(row.status) !== stage &&
                  row.status_transitions?.some(
                    (transition) =>
                      getStatusColumn(transition.status) === stage,
                  ),
              );
              const label = COLUMN_COLORS[stage].label;
              const stageHeader = (
                <>
                  <span className="min-w-0 truncate">{label}</span>
                  <span className="ml-auto text-xs font-medium tabular-nums text-[var(--bz-text-2)]">
                    {column.length}
                  </span>
                </>
              );
              return (
                <section
                  key={stage}
                  aria-label={label}
                  className="min-w-0 snap-start border-t-2 border-[var(--bz-border)]"
                >
                  {completed ? (
                    <h2>
                      <button
                        type="button"
                        className="flex min-h-11 w-full items-center gap-2 rounded-sm text-left text-sm font-semibold text-[var(--bz-text-1)]"
                        onClick={() => setCompletedCollapsed((value) => !value)}
                        aria-expanded={!collapsed}
                        aria-controls={`process-column-${stage}-content`}
                        aria-label={`${label}, ${column.length}, ${collapsed ? "click to expand" : "click to collapse"}`}
                      >
                        {stageHeader}
                        <ChevronDown
                          aria-hidden="true"
                          className={`size-4 ${collapsed ? "-rotate-90" : ""}`}
                        />
                      </button>
                    </h2>
                  ) : (
                    <h2 className="flex min-h-11 items-center gap-2 text-sm font-semibold text-[var(--bz-text-1)]">
                      {stageHeader}
                    </h2>
                  )}
                  <div
                    hidden={collapsed && !isLoading}
                    id={`process-column-${stage}-content`}
                  >
                    {isLoading ? (
                      <div data-testid="loading-skeleton">
                        <SkeletonCard />
                      </div>
                    ) : collapsed ? null : (
                      <>
                        <div className="space-y-5">
                          {column.map((practice) => (
                            <ProcessTile
                              key={practice.id}
                              practice={practice}
                              updating={updatingId === practice.id}
                              openProcess={() =>
                                router.push(`/process/${practice.id}`)
                              }
                              openClient={() =>
                                router.push(`/clients/${practice.client_id}`)
                              }
                              openMenu={handleMenuClick}
                              onContextMenu={handleContextMenu}
                            />
                          ))}
                        </div>
                        {column.length === 0 && (
                          <p className="border-t border-dashed border-[var(--bz-border)] py-3 text-xs text-[var(--bz-text-2)]">
                            No processes
                          </p>
                        )}
                        {history.length > 0 && (
                          <div className="mt-4 border-t border-dashed border-[var(--bz-border)]">
                            <button
                              type="button"
                              className="flex min-h-11 w-full items-center justify-between gap-2 text-xs font-medium text-[var(--bz-text-2)]"
                              onClick={() =>
                                setExpandedGhosts((expanded) => ({
                                  ...expanded,
                                  [stage]: !expanded[stage],
                                }))
                              }
                              aria-expanded={!!expandedGhosts[stage]}
                              aria-controls={`process-history-${stage}`}
                            >
                              Stage history · {history.length}
                              <ChevronDown
                                className={`size-3.5 ${expandedGhosts[stage] ? "rotate-180" : ""}`}
                                aria-hidden="true"
                              />
                            </button>
                            <div
                              id={`process-history-${stage}`}
                              hidden={!expandedGhosts[stage]}
                            >
                              {expandedGhosts[stage] &&
                                history.map((practice) => (
                                  <GhostCard
                                    key={practice.id}
                                    practice={practice}
                                    onClick={() =>
                                      router.push(`/process/${practice.id}`)
                                    }
                                  />
                                ))}
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
        </section>
      )}

      {/* List View */}
      {viewMode === "list" && (
        <div className="bz-product-panel overflow-hidden">
          {isLoading ? (
            <div className="p-12 text-center">
              <div className="animate-pulse space-y-4">
                <div className="h-4 bg-[var(--bz-card)] rounded w-1/3 mx-auto" />
                <div className="h-4 bg-[var(--bz-card)] rounded w-1/2 mx-auto" />
              </div>
            </div>
          ) : filteredPractices.length === 0 ? (
            <div className="flex flex-col items-center justify-center p-12">
              <FolderKanban className="w-16 h-16 text-[var(--bz-text-2)] opacity-50 mb-4" />
              <h3 className="text-lg font-semibold text-[var(--bz-text-1)] mb-2">
                No processes found
              </h3>
              <p className="text-sm text-[var(--bz-text-2)] max-w-md mb-6">
                {activeFiltersCount > 0
                  ? "No processes match the selected filters."
                  : searchQuery
                    ? "No processes match your search. Try different keywords."
                    : "There are no processes for this month."}
              </p>
              {/* P2.3: empty state offers a way out (mirrors clients page) */}
              {activeFiltersCount > 0 ? (
                <Button
                  variant="outline"
                  onClick={clearFilters}
                  className="gap-2"
                >
                  <X className="w-4 h-4" />
                  Clear Filters
                </Button>
              ) : (
                <Button onClick={handleNewCase} className="gap-2">
                  <Plus className="w-4 h-4" />
                  New Process
                </Button>
              )}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-[var(--bz-surface)] border-b border-[var(--bz-border)]">
                  <tr>
                    {(
                      [
                        { key: "id", label: "ID", sort: true },
                        {
                          key: "practice_type_code",
                          label: "Process Type",
                          sort: true,
                        },
                        { key: "client_name", label: "Client", sort: true },
                        {
                          key: "assigned_to",
                          label: "Assigned To",
                          sort: true,
                        },
                        { key: "status", label: "Status", sort: true },
                        { key: "priority", label: "Priority", sort: true },
                        { key: "payment", label: "Payment" },
                        { key: "price", label: "Price", right: true },
                        { key: "updated_at", label: "Updated", sort: true },
                        { key: "actions", label: "Actions" },
                      ] as {
                        key: string;
                        label: string;
                        sort?: boolean;
                        right?: boolean;
                      }[]
                    ).map((col) =>
                      col.sort ? (
                        <th
                          key={col.key}
                          aria-sort={
                            sortField === col.key
                              ? sortOrder === "asc"
                                ? "ascending"
                                : "descending"
                              : "none"
                          }
                          className="px-4 py-3 text-left text-sm font-semibold text-[var(--bz-text-1)] cursor-pointer hover:bg-[var(--bz-base)]/50 transition-colors"
                        >
                          <button
                            type="button"
                            onClick={() => toggleSort(col.key as SortField)}
                            className="flex min-h-8 items-center gap-2 text-left"
                          >
                            {col.label}
                            {sortField === col.key &&
                              (sortOrder === "asc" ? (
                                <ArrowUp className="w-4 h-4" />
                              ) : (
                                <ArrowDown className="w-4 h-4" />
                              ))}
                            {sortField !== col.key && (
                              <ArrowUpDown className="w-4 h-4 opacity-30" />
                            )}
                          </button>
                        </th>
                      ) : (
                        <th
                          key={col.key}
                          className={`px-4 py-3 ${col.right ? "text-right" : "text-left"} text-sm font-semibold text-[var(--bz-text-1)]`}
                        >
                          {col.label}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--bz-border)]">
                  {paginatedPractices.map((practice) => (
                    <tr
                      key={practice.id}
                      className="hover:bg-[var(--bz-card-hover)] transition-colors cursor-pointer"
                      onContextMenu={(e) => handleContextMenu(e, practice)}
                      onClick={() => router.push(`/process/${practice.id}`)}
                    >
                      <td className="px-4 py-3 text-sm font-medium text-[var(--bz-text-1)]">
                        #{practice.id}
                      </td>
                      <td className="px-4 py-3 text-sm text-[var(--bz-text-1)]">
                        {practice.practice_type_code
                          ?.toUpperCase()
                          .replace(/_/g, " ") || "-"}
                      </td>
                      <td className="px-4 py-3 text-sm">
                        {practice.client_id ? (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              router.push(`/clients/${practice.client_id}`);
                            }}
                            className="text-[var(--bz-text-1)] hover:text-[var(--bz-accent)] transition-colors text-left"
                          >
                            {practice.client_name || "Unknown"}
                          </button>
                        ) : (
                          <span className="text-[var(--bz-text-1)]">
                            {practice.client_name || "Unknown"}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm">
                        <span className="inline-flex items-center gap-2">
                          <AssigneeMark practice={practice} />
                          {assigneeLabel(practice)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm">
                        <div className="inline-flex items-center gap-1.5">
                          <span
                            className="w-2 h-2 rounded-full shrink-0"
                            style={statusDotStyle(
                              getStatusColumn(practice.status),
                            )}
                          />
                          <span className="text-[var(--bz-text-1)]">
                            {practice.status
                              ?.replace(/_/g, " ")
                              .charAt(0)
                              .toUpperCase() +
                              practice.status?.replace(/_/g, " ").slice(1) ||
                              "-"}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-sm">
                        {practice.priority === "urgent" ? (
                          <span className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full font-semibold bg-[color-mix(in_srgb,var(--state-danger)_15%,transparent)] text-[var(--state-danger)]">
                            🔥 urgent
                          </span>
                        ) : practice.priority === "high" ? (
                          <span className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full font-semibold bg-[color-mix(in_srgb,var(--state-warning)_12%,transparent)] text-[var(--state-warning)]">
                            ↑ high
                          </span>
                        ) : (
                          <span
                            className="text-[var(--bz-text-2)] text-xs"
                            title="Normal priority"
                            aria-label="Normal priority"
                          >
                            —
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm">
                        {practice.payment_status ? (
                          <span
                            className={`inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full font-medium ${
                              practice.payment_status === "paid"
                                ? "bg-[color-mix(in_srgb,var(--state-success)_15%,transparent)] text-[var(--state-success)]"
                                : practice.payment_status === "partial"
                                  ? "bg-[color-mix(in_srgb,var(--state-warning)_15%,transparent)] text-[var(--state-warning)]"
                                  : "bg-[color-mix(in_srgb,var(--state-danger)_15%,transparent)] text-[var(--state-danger)]"
                            }`}
                          >
                            <span className="w-1.5 h-1.5 rounded-full bg-current" />
                            {practice.payment_status}
                          </span>
                        ) : (
                          <span
                            className="text-[var(--bz-text-2)] text-xs"
                            title="Payment not set"
                            aria-label="Payment not set"
                          >
                            —
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-right">
                        {practice.actual_price || practice.quoted_price ? (
                          <Money
                            compact
                            value={
                              practice.actual_price ||
                              practice.quoted_price ||
                              0
                            }
                            className="font-medium"
                            style={{ color: "var(--bz-accent)" }}
                          />
                        ) : (
                          <span className="text-[var(--bz-text-2)] text-xs">
                            —
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm">
                        {practice.updated_at ? (
                          (() => {
                            const ageDays = Math.floor(
                              (Date.now() -
                                new Date(practice.updated_at).getTime()) /
                                86400000,
                            );
                            const label =
                              ageDays === 0
                                ? "today"
                                : ageDays >= 30
                                  ? `${Math.floor(ageDays / 30)}mo`
                                  : ageDays >= 7
                                    ? `${Math.floor(ageDays / 7)}w`
                                    : `${ageDays}d`;
                            return (
                              <span
                                className="text-xs tabular-nums px-1.5 py-0.5 rounded"
                                style={{
                                  background:
                                    ageDays > 14
                                      ? "color-mix(in srgb, var(--state-danger) 12%, transparent)"
                                      : ageDays > 7
                                        ? "color-mix(in srgb, var(--state-warning) 10%, transparent)"
                                        : "transparent",
                                  color:
                                    ageDays > 14
                                      ? "var(--state-danger)"
                                      : ageDays > 7
                                        ? "var(--state-warning)"
                                        : "var(--bz-text-2)",
                                }}
                                title={`Last updated: ${new Date(practice.updated_at).toLocaleDateString("en-US")}`}
                                aria-label={`Last updated: ${new Date(practice.updated_at).toLocaleDateString("en-US")}`}
                              >
                                {label}
                              </span>
                            );
                          })()
                        ) : (
                          <span className="text-[var(--bz-text-2)] text-xs">
                            —
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm">
                        <div
                          className="flex items-center gap-1"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <ContactQuickActions practice={practice} />
                          <button
                            className="p-1.5 rounded text-[var(--bz-text-2)] hover:text-[var(--bz-text-1)] hover:bg-[var(--bz-card-hover)] transition-colors"
                            onClick={(e) => handleMenuClick(e, practice)}
                            title="More options"
                            aria-label="More options"
                          >
                            <MoreVertical className="w-4 h-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {/* Pagination Controls */}
              {totalPages > 1 && (
                <div className="flex items-center justify-between px-4 py-3 border-t border-[var(--bz-border)] bg-[var(--bz-surface)]">
                  <div className="text-sm text-[var(--bz-text-2)]">
                    Showing {(listPageNumber - 1) * itemsPerPage + 1} to{" "}
                    {Math.min(
                      listPageNumber * itemsPerPage,
                      filteredPractices.length,
                    )}{" "}
                    of {filteredPractices.length} process
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() =>
                        setListPageNumber((p) => Math.max(1, p - 1))
                      }
                      disabled={listPageNumber === 1}
                      className="px-3 py-1 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-surface)] text-[var(--bz-text-1)] hover:bg-[var(--bz-base)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      Previous
                    </button>
                    <span className="self-center whitespace-nowrap px-2 text-sm tabular-nums text-[var(--bz-text-2)]">
                      Page {listPageNumber} of {totalPages}
                    </span>
                    <button
                      onClick={() =>
                        setListPageNumber((p) => Math.min(totalPages, p + 1))
                      }
                      disabled={listPageNumber === totalPages}
                      className="px-3 py-1 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-surface)] text-[var(--bz-text-1)] hover:bg-[var(--bz-base)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Context Menu for Status Change */}
      {selectedPractice && menuPosition && (
        <div
          ref={menuRef}
          className="fixed z-50 w-[230px] overflow-y-auto rounded-lg border border-[var(--bz-border)] bg-[var(--bz-card)] shadow-[var(--bz-shadow-card)] py-1 animate-in fade-in zoom-in-95 duration-100"
          style={{
            top:
              menuPosition.y + 440 > window.innerHeight
                ? Math.max(10, window.innerHeight - 450) // Open above if not enough space below
                : menuPosition.y,
            left:
              menuPosition.x + 230 > window.innerWidth
                ? Math.max(10, window.innerWidth - 240) // Open to left if not enough space to right
                : menuPosition.x,
            // Ensure menu never goes offscreen
            maxHeight: "min(440px, calc(100dvh - 20px))",
            maxWidth: "calc(100vw - 20px)",
          }}
        >
          <div className="px-3 py-2 border-b border-[var(--bz-border)] bg-[var(--bz-surface)] rounded-t-lg">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs font-semibold text-[var(--bz-text-1)]">
                  {selectedPractice.can_edit_process
                    ? "Update Status"
                    : "Process details"}
                </p>
                <p className="text-[11px] text-[var(--bz-text-2)]">
                  {selectedPractice.practice_type_code
                    ?.toUpperCase()
                    .replace(/_/g, " ") || "Process"}{" "}
                  #{selectedPractice.id}
                </p>
              </div>
              <button
                onClick={() => {
                  router.push(`/process/${selectedPractice.id}`);
                  setSelectedPractice(null);
                  setMenuPosition(null);
                }}
                className="text-[11px] px-2 py-1 rounded border border-[var(--bz-border)] text-[var(--bz-text-2)] hover:text-[var(--bz-text-1)] hover:border-[var(--bz-accent)]/40 transition-colors"
              >
                Open →
              </button>
            </div>
          </div>
          {selectedPractice.can_edit_process && (
            <div className="max-h-[300px] overflow-y-auto p-1 space-y-0.5">
              {STATUS_OPTIONS.filter(
                (option) => !managedStageReason(option.value),
              ).map((option) => {
                const needsPrice = needsQuotedPriceBefore(
                  option.value,
                  selectedPractice.quoted_price,
                  selectedPractice.status,
                );
                return (
                  <button
                    key={option.value}
                    title={
                      needsPrice ? QUOTED_PRICE_REQUIRED_TEXT_ID : undefined
                    }
                    onClick={() =>
                      handleStatusChange(selectedPractice.id, option.value)
                    }
                    disabled={
                      selectedPractice.status === option.value ||
                      needsPrice ||
                      updatingId !== null
                    }
                    className={`w-full px-3 py-2 text-left text-sm rounded-md transition-colors flex items-center justify-between group ${
                      selectedPractice.status === option.value
                        ? "bg-[var(--bz-accent)]/10 text-[var(--bz-accent)] font-medium"
                        : "text-[var(--bz-text-1)] hover:bg-[var(--bz-accent)]/10 hover:text-[var(--bz-accent)]"
                    } ${updatingId !== null || needsPrice ? "opacity-50 cursor-not-allowed" : ""}`}
                  >
                    <div className="flex flex-col">
                      <span className="flex items-center gap-2">
                        <span
                          className="w-2 h-2 rounded-full"
                          style={statusDotStyle(option.column)}
                        />
                        {option.label}
                      </span>
                      {needsPrice && (
                        <span className="text-[11px] text-[var(--bz-text-2)] pl-4">
                          {QUOTED_PRICE_REQUIRED_TEXT_ID}
                        </span>
                      )}
                    </div>
                    {selectedPractice.status === option.value && (
                      <CheckCircle2 className="w-3.5 h-3.5" />
                    )}
                  </button>
                );
              })}
            </div>
          )}
          <div className="border-t border-[var(--bz-border)] px-3 py-2 text-sm">
            Payment ·{" "}
            <span className="capitalize">
              {selectedPractice.payment_status || "unpaid"}
            </span>
          </div>
          <div className="flex items-center gap-2 border-t border-[var(--bz-border)] px-3 py-2">
            <ContactQuickActions practice={selectedPractice} />
            {selectedPractice.client_id > 0 && (
              <button
                type="button"
                onClick={() =>
                  router.push(
                    `/clients/${selectedPractice.client_id}?tab=documents`,
                  )
                }
                className="inline-flex min-h-9 items-center gap-1 text-xs text-[var(--bz-text-1)]"
              >
                <FileText aria-hidden="true" className="size-4" />
                Documents
              </button>
            )}
          </div>
          {/* Priority Section */}
          {selectedPractice.can_edit_process && (
            <div className="border-t border-[var(--bz-border)] px-3 py-2">
              <p className="text-[11px] text-[var(--bz-text-2)] mb-1.5 font-medium uppercase tracking-wide">
                Priority
              </p>
              <div className="flex gap-1">
                {(
                  [
                    {
                      value: "normal",
                      label: "Normal",
                      color: "var(--bz-text-secondary)",
                      active:
                        "color-mix(in srgb, var(--bz-text-secondary) 25%, transparent)",
                      border:
                        "color-mix(in srgb, var(--bz-text-secondary) 40%, transparent)",
                    },
                    {
                      value: "high",
                      label: "High",
                      color: "var(--state-warning)",
                      active:
                        "color-mix(in srgb, var(--state-warning) 25%, transparent)",
                      border:
                        "color-mix(in srgb, var(--state-warning) 40%, transparent)",
                    },
                    {
                      value: "urgent",
                      label: "Urgent",
                      color: "var(--state-danger)",
                      active:
                        "color-mix(in srgb, var(--state-danger) 25%, transparent)",
                      border:
                        "color-mix(in srgb, var(--state-danger) 40%, transparent)",
                    },
                  ] as const
                ).map((p) => {
                  const isCurrent =
                    (selectedPractice.priority || "normal") === p.value;
                  return (
                    <button
                      key={p.value}
                      onClick={() =>
                        handlePriorityChange(selectedPractice.id, p.value)
                      }
                      disabled={isCurrent || updatingId !== null}
                      className="flex-1 text-[11px] py-1 rounded font-medium transition-all disabled:opacity-40 disabled:cursor-default capitalize"
                      style={{
                        background: isCurrent ? p.active : "var(--bz-surface)",
                        color: isCurrent ? p.color : "var(--bz-text-2)",
                        border: isCurrent
                          ? `1px solid ${p.border}`
                          : "1px solid var(--bz-border)",
                      }}
                    >
                      {p.label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

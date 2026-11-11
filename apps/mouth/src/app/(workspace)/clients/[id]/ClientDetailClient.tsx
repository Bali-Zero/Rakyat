"use client";

import React, { useCallback, useState, useEffect, useRef } from "react";
import { scrollActiveTabIntoView } from "./tabBarScroll";
import dynamic from "next/dynamic";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  User,
  FileText,
  DollarSign,
  Globe,
  Users,
  FolderOpen,
  Building2,
  AlertCircle,
  Bell,
  MessageCircle,
  Loader2,
  AlertTriangle,
  Mail,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { AvatarWithFallback } from "@/components/ui/avatar-with-fallback";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { logger } from "@/lib/logger";
import type {
  FamilyMember,
  ClientDocument,
  Interaction,
} from "@/lib/api/crm/crm.types";
import { getCountryFlag } from "@/lib/utils/nationality-flags";
import {
  useClientDetail,
  useClientBusinessStory,
  useClientTimeline,
  useDocumentCategories,
  useInvalidateClient,
  useInvalidateClientProfile,
  useSetClientCache,
} from "@/hooks/useClientDetail";
import { useClickOutside } from "@/hooks/useClickOutside";
import {
  Masthead,
  StatePill,
  Stamp,
  PILL_TONE,
  PILL_SQUARE,
  LedgerSection,
  EmptyState,
  Notice,
  EYEBROW,
  FIELD,
} from "@/components/workspace/r19";
import {
  clientStatusTone,
  viewerCanDeleteClient,
  viewerIsNext,
} from "../client-row-model";
import styles from "./client-detail-desk.module.css";
import { formatCardDate, isLive } from "./components/documentValidity";
import { DeleteClientDialog } from "./components/DeleteClientDialog";
import { useProfileDay, useWitaClock } from "./components/witaClock";
import { FOCUS } from "@/components/workspace/r19/tokens";
import { lkpmApi } from "@/lib/api/workspace/lkpm.api";

// Local component imports
import { companyDocumentToClientDocument } from "./components/companyDocumentToClientDocument";
import { isTabType } from "./components/types";
import type { TabType, ModalType } from "./components/types";
import type { TaxConsultantOption } from "@/lib/workspace/roster-directory";
import { useTeamMemberOptions } from "@/hooks/useTeamMembers";
import { formatCurrency } from "./components/utils";
import { OverviewTab, overviewLedgerCount } from "./components/OverviewTab";
import { initials } from "./components/overview-brief";
import {
  firstName,
  legacyConsultantLabel,
  permitLine,
  roleLine,
} from "./components/header-facts";
import { clearCompanyDetailCache } from "./components/company/companyDetailCache";
import { BusinessStoryPanel } from "./components/BusinessStoryPanel";
import { viewerCanChangeDocumentVisibility } from "./components/modals/document-visibility";
import { startEarly, earlyKey } from "./components/early-fetch";
import { TabSkeleton } from "./components/TabSkeleton";

/*
 * Only Overview is in this chunk. Every other tab and every modal is its own
 * chunk, fetched when it is first needed — or a moment before, on hover/focus
 * of its tab button (`TAB_LOADERS`) — so the first paint no longer waits on
 * ~250 KB of code the Overview never runs. Each loader is the SAME
 * `import()` the dynamic component uses, so a preload and the render share one
 * network request.
 */
/** One import() per chunk, shared by a hover preload and the render. */
const once = <T,>(load: () => Promise<T>) => {
  let promise: Promise<T> | undefined;
  return () => (promise ??= load());
};

const loadDocuments = once(() =>
  import("./components/DocumentsTab").then((m) => m.DocumentsTab),
);
const loadFiles = once(() =>
  import("./components/FilesBrowserPanel").then((m) => m.FilesBrowserPanel),
);
const loadProcess = once(() =>
  import("./components/ProcessTab").then((m) => m.ProcessTab),
);
const loadFamily = once(() =>
  import("./components/FamilyTab").then((m) => m.FamilyTab),
);
const loadImmigration = once(() =>
  import("./components/ImmigrationTab").then((m) => m.ImmigrationTab),
);
const loadCompany = once(() =>
  import("./components/CompanyTab").then((m) => m.CompanyTab),
);
const loadTax = once(() => import("./components/TaxTab").then((m) => m.TaxTab));

/** Per tab key: how to fetch its chunk ahead of the click. Overview is static. */
export const TAB_LOADERS: Partial<Record<TabType, () => Promise<unknown>>> = {
  process: loadProcess,
  visas: loadImmigration,
  company: loadCompany,
  tax: loadTax,
  family: loadFamily,
  documents: () => Promise.all([loadDocuments(), loadFiles()]),
};

const DocumentsTab = dynamic(loadDocuments, { loading: () => <TabSkeleton /> });
const FilesBrowserPanel = dynamic(loadFiles, { loading: () => null });
const ProcessTab = dynamic(loadProcess, { loading: () => <TabSkeleton /> });
const FamilyTab = dynamic(loadFamily, { loading: () => <TabSkeleton /> });
const ImmigrationTab = dynamic(loadImmigration, {
  loading: () => <TabSkeleton />,
});
const CompanyTab = dynamic(loadCompany, { loading: () => <TabSkeleton /> });
const TaxTab = dynamic(loadTax, { loading: () => <TabSkeleton /> });

// Modals open on a click and draw their own overlay: nothing to show while the
// chunk loads (it is a few ms on a warm cache), so `loading` is empty.
const EditClientModal = dynamic(
  () =>
    import("./components/modals/EditClientModal").then(
      (m) => m.EditClientModal,
    ),
  { loading: () => null },
);
const AddFamilyMemberModal = dynamic(
  () =>
    import("./components/modals/AddFamilyMemberModal").then(
      (m) => m.AddFamilyMemberModal,
    ),
  { loading: () => null },
);
const EditFamilyMemberModal = dynamic(
  () =>
    import("./components/modals/EditFamilyMemberModal").then(
      (m) => m.EditFamilyMemberModal,
    ),
  { loading: () => null },
);
const AddDocumentModal = dynamic(
  () =>
    import("./components/modals/AddDocumentModal").then(
      (m) => m.AddDocumentModal,
    ),
  { loading: () => null },
);
const EditDocumentModal = dynamic(
  () =>
    import("./components/modals/EditDocumentModal").then(
      (m) => m.EditDocumentModal,
    ),
  { loading: () => null },
);
const AddCompanyModal = dynamic(
  () =>
    import("./components/modals/AddCompanyModal").then(
      (m) => m.AddCompanyModal,
    ),
  { loading: () => null },
);

/**
 * The client-status trigger (below) is a menu TRIGGER, not a report — it
 * needs an `aria-label` and a click handler `StatePill` has no slot for
 * (K3a's own `StatePill` is either an inert `<span>` or an
 * `aria-pressed` filter button, neither of which carries a caller
 * `aria-label`). Rather than fork the primitive or copy its class strings,
 * this reuses its own exported tone table (`PILL_TONE` + `PILL_SQUARE`) on a
 * native button, so the trigger's colour still goes through
 * `clientStatusTone` — the r19 tone vocabulary, just not the literal
 * component. Every OTHER status word on this page (the menu's own options,
 * below) IS the real `StatePill`.
 */
const STATUS_TRIGGER_BASE =
  "inline-flex h-6 items-center gap-1.5 whitespace-nowrap border px-2.5 " +
  PILL_SQUARE +
  " text-[10px] font-[650] uppercase tracking-[0.12em] bg-transparent cursor-pointer hover:opacity-80 transition-opacity disabled:cursor-wait";

/** Masthead layout for this page: column below xl, row from xl (today's desktop). */
export const MASTHEAD_STACK =
  "flex-1 min-w-0 md:flex-col md:items-start md:gap-3 xl:flex-row xl:items-end xl:gap-[18px]";

export interface ClientDetailClientProps {
  /**
   * The assignable tax team, resolved on the SERVER and handed down.
   *
   * It exists as a prop for one reason: the five addresses used to be a literal
   * inside `components/TaxTab.tsx`, and a literal in a "use client" module is
   * compiled into this route's static chunk, which is served with no session.
   * Passing it means the names live in the server payload for this route instead
   * of in a file any anonymous caller can GET by path.
   */
  taxConsultants: readonly TaxConsultantOption[];
}

export function ClientDetailClient({
  taxConsultants,
}: ClientDetailClientProps) {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const clientId = params?.id ? Number(params.id) : 0;
  const { options: teamMemberOptions } = useTeamMemberOptions();

  // The calls that only need the client id start NOW, in parallel with the
  // profile request, not after it resolves and the Overview mounts. Their
  // owning components take these promises on first load (early-fetch.ts).
  useEffect(() => {
    if (!clientId) return;
    startEarly(earlyKey("ai-summary", clientId), () =>
      api.crm.getClientAiSummary(clientId),
    );
    startEarly(earlyKey("wa-case-intel", clientId), () =>
      api.crm.getClientWaCaseIntelligence(clientId),
    );
    startEarly(earlyKey("portal-status", clientId), () =>
      api.crm.getPortalStatus(clientId),
    );
    startEarly(earlyKey("portal-messages", clientId), () =>
      api.crm.getPortalMessages(clientId),
    );
  }, [clientId]);

  const {
    data: profile,
    isLoading,
    error: queryError,
  } = useClientDetail(clientId);
  // The card's day is the payload's `as_of`; the browser clock only asks for
  // a fresh payload once its WITA day has moved past it (WitaClock).
  const profileDay = useProfileDay(profile);
  const invalidateProfile = useInvalidateClientProfile(clientId);
  useWitaClock(profileDay, () => void invalidateProfile());
  const { data: timelineData } = useClientTimeline(clientId);
  const { data: docCategoriesData } = useDocumentCategories();
  const invalidateClientQueries = useInvalidateClient(clientId);
  // Any change made from this card (a document, a family member, a company
  // field) also drops the cached company detail the Company tab keeps.
  const invalidateClient = useCallback(() => {
    clearCompanyDetailCache();
    return invalidateClientQueries();
  }, [invalidateClientQueries]);
  const setClientCache = useSetClientCache(clientId);
  const businessStoryQuery = useClientBusinessStory(
    clientId,
    profile?.client.full_name ?? "",
    profile?.company_links,
  );

  // The newest logged contact feeds the Client info card. The Activity tab is
  // gone, so nothing on this page writes interactions any more: it is a plain
  // read of the timeline query.
  const interactions: Interaction[] = timelineData ?? [];

  const docCategories = docCategoriesData ?? [];
  const error = queryError ? "Failed to load client data" : null;

  // Mirror query error to toast (matches original Promise.all error behavior)
  useEffect(() => {
    if (queryError) {
      logger.error("Failed to load client data:", {}, queryError as Error);
      toast.error("Failed to load client data");
    }
  }, [queryError]);

  const [activeTab, setActiveTab] = useState<TabType>("overview");
  const [activeModal, setActiveModal] = useState<ModalType>("none");
  const [editingDocument, setEditingDocument] = useState<ClientDocument | null>(
    null,
  );
  const [editingFamilyMember, setEditingFamilyMember] =
    useState<FamilyMember | null>(null);
  const [isMounted, setIsMounted] = useState(false);
  // Which Documents group opens first: "removed" when a signpost on another
  // tab sent the reader there.
  const [documentsGroup, setDocumentsGroup] = useState<string>("all");
  // The signed-in viewer, for the ownership predicate only (copper stamp,
  // masthead subtitle) — read once, same as `api.getUserProfile()` in K3a's
  // /clients desk. `null`/no-email means "viewer unknown": no copper, no
  // subtitle, per concept.md §6.
  const [currentUserEmail, setCurrentUserEmail] = useState<string>("");
  // CRM-18: the viewer's role, read from the same profile, for the
  // delete-permission predicate only. Unknown = no admin path.
  const [currentUserRole, setCurrentUserRole] = useState<string>("");
  useEffect(() => {
    let mounted = true;
    const viewerProfile = api.getUserProfile?.();
    if (viewerProfile?.email) {
      setCurrentUserEmail(viewerProfile.email);
      setCurrentUserRole(viewerProfile.role ?? "");
      return;
    }
    // Cache miss — fall back to one network read. Silent failure means the
    // viewer stays unknown (no copper, no subtitle), never a thrown error;
    // the unmount guard stops a late resolve from touching an unmounted
    // desk (round-3 R5).
    api
      .getProfile()
      .then((user) => {
        if (mounted && user?.email) {
          setCurrentUserEmail(user.email);
          setCurrentUserRole(user.role ?? "");
        }
      })
      .catch(() => {
        // stays unknown — see docstring above
      });
    return () => {
      mounted = false;
    };
  }, []);
  const [isDeletingClient, setIsDeletingClient] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [showStatusMenu, setShowStatusMenu] = useState(false);
  const [isUpdatingStatus, setIsUpdatingStatus] = useState(false);
  // R7a: the two controls the hidden Company/Tax tabs used to own for a
  // company-less client — linking the first company and assigning the tax
  // consultant. Kept on Overview so hiding the tabs orphans no action.
  const [isAddingCompany, setIsAddingCompany] = useState(false);
  const [taxConsultantValue, setTaxConsultantValue] = useState("");
  const [isSavingTaxConsultant, setIsSavingTaxConsultant] = useState(false);
  useEffect(() => {
    setTaxConsultantValue(profile?.client.tax_consultant ?? "");
  }, [profile?.client.tax_consultant]);
  // A tax client: a consultant on record, or LKPM history (the only live tax
  // data — the tax_* tables are empty in prod). Unknown until it answers.
  const [lkpmReports, setLkpmReports] = useState(0);
  useEffect(() => {
    let alive = true;
    lkpmApi
      .getClientHistory(clientId)
      .then((r) => {
        if (alive) setLkpmReports(r.count);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [clientId]);
  const isTaxClient = Boolean(taxConsultantValue) || lkpmReports > 0;
  // The team block names people by FIRST NAME only.
  const offeredConsultant = taxConsultants.find(
    (c) => c.value === taxConsultantValue,
  );
  const taxConsultantLabel = taxConsultantValue
    ? offeredConsultant
      ? firstName(offeredConsultant.label, taxConsultantValue)
      : legacyConsultantLabel(taxConsultantValue)
    : "";

  const saveTaxConsultant = async (newValue: string) => {
    const previous = taxConsultantValue;
    setTaxConsultantValue(newValue);
    setIsSavingTaxConsultant(true);
    try {
      const user = await api.getProfile();
      await api.crm.updateClient(
        clientId,
        { tax_consultant: newValue || null },
        user.email,
      );
      toast.success(
        newValue
          ? `Tax consultant: ${taxConsultants.find((c) => c.value === newValue)?.label ?? newValue}`
          : "Tax consultant cleared",
      );
      void invalidateClient();
    } catch (err) {
      setTaxConsultantValue(previous); // revert on error, same as TaxTab
      toast.error("Failed to update tax consultant", {
        description: (err as Error).message,
      });
    } finally {
      setIsSavingTaxConsultant(false);
    }
  };
  const tabsRef = useRef<HTMLElement>(null);
  // The tab bar scrolls inside itself on narrow screens: keep the active tab in view.
  useEffect(() => {
    scrollActiveTabIntoView(tabsRef.current);
  }, [activeTab, isLoading]);
  // Set by the Documents tab's passport hint: once Overview has rendered,
  // bring its Passport card into view. Runs after handleTabChange's own
  // scroll restore (that rAF is queued first), so it is not undone.
  const [scrollToPassport, setScrollToPassport] = useState(false);
  useEffect(() => {
    if (!scrollToPassport || activeTab !== "overview") return;
    setScrollToPassport(false);
    requestAnimationFrame(() => {
      document
        .getElementById("passport-card")
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, [scrollToPassport, activeTab]);
  const statusMenuRef = useRef<HTMLDivElement>(null);
  useClickOutside(
    statusMenuRef,
    () => setShowStatusMenu(false),
    showStatusMenu,
  );

  const updateStatus = async (newStatus: string) => {
    if (newStatus === profile?.client.status) {
      setShowStatusMenu(false);
      return;
    }
    setIsUpdatingStatus(true);
    setShowStatusMenu(false);
    try {
      const user = await api.getProfile();
      const updatedClient = await api.crm.updateClient(
        clientId,
        { status: newStatus },
        user.email,
      );
      setClientCache(updatedClient);
      void invalidateClient();
      toast.success(`Status updated to ${newStatus}`);
    } catch (err) {
      toast.error("Failed to update status", {
        description: (err as Error).message,
      });
    } finally {
      setIsUpdatingStatus(false);
    }
  };

  // Fix hydration mismatch: only render dates on client
  useEffect(() => {
    setIsMounted(true);
  }, []);

  // Read tab from URL params and set active tab
  useEffect(() => {
    const tabParam = searchParams?.get("tab");
    if (isTabType(tabParam)) {
      setActiveTab(tabParam);
    }
  }, [searchParams]);

  const openRemovedDocuments = () => {
    setDocumentsGroup("removed");
    handleTabChange("documents");
  };

  const handleTabChange = (tab: TabType, alreadyActive?: boolean) => {
    // A click on the tab that is already showing is a true no-op only when
    // `activeTab` and `visibleTab` AGREE. The render-time fallback
    // (`visibleTab`, below) can force them apart — a deep link to a hidden
    // Company/Tax tab shows Overview while `activeTab` and the URL still say
    // "company". There, clicking the visible Overview button is the one
    // gesture that normalises both; skipping it would leave the URL lying and
    // `activeTab` stuck, so the next render that flips `showCompanyTab` /
    // `showTaxTab` true (e.g. Add company) would jump the panel with no user
    // action. (A retired `?tab=timeline|whatsapp` is not a TabType: the
    // reader ignores it and the page stays on Overview.)
    if (alreadyActive && activeTab === visibleTab) return;
    const scrollY = window.scrollY;
    setActiveTab(tab);
    router.replace(`/clients/${params.id}?tab=${tab}`, { scroll: false });
    // Restore scroll position after React re-render settles
    requestAnimationFrame(() => {
      window.scrollTo(0, scrollY);
    });
  };

  const formatDate = (dateStr: string) => formatCardDate(dateStr, isMounted);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="w-8 h-8 animate-spin text-[var(--tx-secondary)]" />
      </div>
    );
  }

  if (error || !profile) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px] gap-4">
        <AlertTriangle className="w-12 h-12 text-[var(--state-warning)]" />
        <p className="text-[var(--bz-text-2)]">{error || "Client not found"}</p>
        <Button variant="outline" onClick={() => router.push("/clients")}>
          Back to Clients
        </Button>
      </div>
    );
  }

  const {
    client,
    family_members,
    documents,
    practices,
    company_links,
    stats,
    attention_stats: attentionStats,
  } = profile;
  const setupOption = client.assigned_to
    ? teamMemberOptions.find(
        (m) => m.value === client.assigned_to!.toLowerCase(),
      )
    : undefined;
  const setupName = client.assigned_to
    ? firstName(setupOption?.label, client.assigned_to)
    : "";

  // General docs only — excludes passports (Overview), immigration (Immigration tab), company docs (Company tab)
  const isPassportDoc = (d: ClientDocument) =>
    d.document_type?.toLowerCase().includes("passport");
  const isImmigrationDoc = (d: ClientDocument) =>
    d.document_category === "immigration" ||
    d.document_type?.toLowerCase().includes("kitas") ||
    d.document_type?.toLowerCase().includes("kitap") ||
    d.document_type?.toLowerCase().includes("visa") ||
    d.document_type?.toLowerCase().includes("permit") ||
    d.document_type?.toLowerCase().includes("imta") ||
    d.document_type?.toLowerCase().includes("rptka") ||
    d.document_type?.toLowerCase().includes("evisa") ||
    d.document_type?.toLowerCase().includes("voa");
  const isCompanyDoc = (d: ClientDocument) => d.document_category === "pma";
  // The row-23 guard: only LIVE general documents are grouped by category.
  const generalDocuments = documents.filter(
    (d) =>
      !isPassportDoc(d) &&
      !isImmigrationDoc(d) &&
      !isCompanyDoc(d) &&
      isLive(d),
  );
  // Every void document of the profile (removed, archived or rejected), of
  // whatever kind, is listed exactly once, in the Documents tab's "Removed or
  // rejected" group; the domain tabs list live documents only.
  const voidDocuments = documents.filter((d) => !isLive(d));
  const documentRows = [...generalDocuments, ...voidDocuments];
  // Live passports are left out of the Documents tab (they show in Overview ›
  // Passport); counted here so the tab can say where they went.
  const passportDocs = documents.filter((d) => isPassportDoc(d) && isLive(d));

  // Group documents by category
  const documentsByCategory = generalDocuments.reduce(
    (acc, doc) => {
      const cat = doc.document_category || "other";
      if (!acc[cat]) acc[cat] = [];
      acc[cat].push(doc);
      return acc;
    },
    {} as Record<string, ClientDocument[]>,
  );

  // Seam 3 (4-Model Consensus): merge profile.company_documents into TaxTab's documents
  // so corporate SPTs (spt_company/spt_tahunan_badan/bpe_spt_badan), Coretax credentials,
  // and corporate EFINs on linked PTs light up TaxTab without duplicating rows in documents.
  const taxDocuments: ClientDocument[] = [
    ...documents,
    ...(profile.company_documents ?? []).map((cd) =>
      companyDocumentToClientDocument(cd, clientId),
    ),
  ];

  // Calculate stats
  const activePractices = practices.filter(
    (p) => !["completed", "cancelled", "approved"].includes(p.status),
  );
  const completedPractices = practices.filter((p) =>
    ["completed", "approved"].includes(p.status),
  );
  const businessStoryCompanyNames = (company_links ?? [])
    .map((link) => link.company_name)
    .filter((companyName) => companyName.trim().length > 0);
  const businessStoryError =
    businessStoryQuery.error instanceof Error
      ? businessStoryQuery.error
      : businessStoryQuery.error
        ? new Error("Business story request failed")
        : null;

  // R7a: hide the Company and Tax tabs for a client with no company links.
  // The decision is made only on the LOADED profile — while `useClientDetail`
  // is unresolved the page returns the spinner before the tab bar exists,
  // so there is no window in which an `undefined`/empty `company_links` could
  // flicker the tabs out. A client whose record still names a company keeps
  // the tabs even with an empty link list: CompanyTab's name-search fallback
  // then surfaces real company data (and its Sync Drive / edit / vault
  // actions) that hiding would orphan.
  const hasCompanyLinks = (company_links?.length ?? 0) > 0;
  const showCompanyTab = hasCompanyLinks || Boolean(client.company_name);
  // R7a rework: the Tax tab also stays when the CLIENT'S OWN tax identifiers
  // are on record (TaxTab renders them — `client.npwp ?? client.tax_id` and
  // `client.nib` — and nothing else on the page does), so hiding the tab for
  // a company-less client with a personal NPWP would make a stored fact
  // invisible.
  const showTaxTab =
    showCompanyTab || Boolean(client.npwp || client.tax_id || client.nib);
  // Render-time fallback, NOT a URL-effect reset: the effect runs while the
  // profile is still loading, so bouncing there would discard a deep link
  // before the data can prove whether the tab exists.
  const visibleTab: TabType =
    (activeTab === "company" && !showCompanyTab) ||
    (activeTab === "tax" && !showTaxTab)
      ? "overview"
      : activeTab;

  // Get country flag for fallback
  const countryFlag = getCountryFlag(client.nationality);

  // Masthead subtitle — DISAPPEARS when its data cannot prove it
  // (concept.md §6): no sentence with zero active practices, none while the
  // viewer is unknown either (the ownership half would silently read false).
  const needsViewerAction = Boolean(
    currentUserEmail && viewerIsNext(client, currentUserEmail),
  );

  // CRM-18. The backend is the real fence — `verify_client_access(..,
  // write=True)` (crm_utils.py:169-232) 403s a viewer who is neither admin
  // nor owner, whatever this desk renders. This predicate only decides
  // whether to OFFER the action; it is narrower than the fence on purpose
  // (see viewerCanDeleteClient) and, while the viewer is unknown, false.
  const canDeleteClient = viewerCanDeleteClient(
    client,
    currentUserEmail,
    currentUserRole,
  );

  // Soft delete: status -> inactive + deleted_at set, so list queries drop
  // the record (crm_clients.py:1838-1847). Nothing is destroyed, but the
  // record leaves every list, so CRM-18 asks for two deliberate steps:
  // DeleteClientDialog explains, then wants the client's name typed.
  const confirmDeleteClient = () => {
    if (isDeletingClient) return;
    setDeleteDialogOpen(true);
  };
  const deleteClient = async () => {
    if (isDeletingClient) return;
    setIsDeletingClient(true);
    try {
      await api.crm.deleteClient(clientId, currentUserEmail);
      toast.success("Client marked inactive");
      setDeleteDialogOpen(false);
      router.push("/clients");
    } catch (err) {
      toast.error("Error", {
        description: (err as Error).message,
      });
      setIsDeletingClient(false);
    }
  };
  const clientMastheadSubtitle =
    activePractices.length > 0
      ? `${activePractices.length} ${activePractices.length === 1 ? "process is" : "processes are"} moving${
          needsViewerAction ? "; this record needs your action." : "."
        }`
      : undefined;

  // Eyebrow = the record reference only; the company moved under the name, as
  // the client's role in it.
  const clientRefEyebrow = `CLIENT · #${String(client.id).padStart(4, "0")}`;
  const headerRole = roleLine(company_links);
  const headerPermit = permitLine(documents);
  const headerSub =
    headerRole || headerPermit || clientMastheadSubtitle ? (
      <>
        {headerRole ? (
          <span className="block" data-testid="header-role">
            {headerRole}
          </span>
        ) : null}
        {headerPermit ? (
          <span className="block" data-testid="header-permit">
            {headerPermit}
          </span>
        ) : null}
        {clientMastheadSubtitle ? (
          <span className="block">{clientMastheadSubtitle}</span>
        ) : null}
      </>
    ) : undefined;

  const clientTone = clientStatusTone(client.status);
  const overviewLedgerN = overviewLedgerCount(
    documents,
    profile.attention,
    activePractices,
    profile.attention_error,
    profileDay!,
  );
  const businessStoryShown = (businessStoryQuery.data?.length ?? 0) > 0;

  return (
    <div className="space-y-6">
      {canDeleteClient ? (
        <DeleteClientDialog
          open={deleteDialogOpen}
          clientName={client.full_name}
          isDeleting={isDeletingClient}
          onOpenChange={setDeleteDialogOpen}
          onConfirm={deleteClient}
        />
      ) : null}
      {/* Back */}
      <Button
        variant="ghost"
        size="icon"
        onClick={() => router.back()}
        aria-label="Go back"
      >
        <ArrowLeft className="w-5 h-5" />
      </Button>

      {/* Masthead — reference eyebrow, name, a computed sentence that
          disappears when its data cannot prove it (concept.md §6). The
          avatar has no r19 slot, so it wraps the primitive page-locally
          rather than forking it (per spec §"Hard rules"). */}
      <div className="flex items-start gap-4">
        <div className="relative w-16 h-16 shrink-0">
          <div className="w-16 h-16 rounded-full bg-[var(--bz-card)] border border-[var(--bz-border)] flex items-center justify-center overflow-hidden">
            {client.avatar_url ? (
              <img
                src={client.avatar_url}
                alt={client.full_name}
                className="w-full h-full object-cover"
              />
            ) : (
              <span
                aria-hidden="true"
                className="text-[24px] font-semibold tracking-[-0.02em] text-[var(--tx-pure)]"
              >
                {initials(client.full_name)}
              </span>
            )}
          </div>
          {countryFlag && (
            <span
              className="absolute -bottom-0.5 -right-0.5 flex h-6 w-6 items-center justify-center rounded-full border border-[var(--bz-border)] bg-[var(--bz-base)] text-[14px] leading-none"
              title={client.nationality}
              aria-label={client.nationality}
            >
              {countryFlag}
            </span>
          )}
        </div>
        <Masthead
          // Stacked (actions BELOW the title block) until xl: at 768 with the
          // sidebar open, title + role + permit and four actions side by
          // side leave the title one word per line. The shared Masthead is
          // untouched; its md:flex-row is overridden here via tailwind-merge.
          className={MASTHEAD_STACK}
          eyebrow={clientRefEyebrow}
          title={client.full_name}
          subtitle={headerSub}
          right={
            <>
              {/* Copper "NEEDS YOU" stamp — derived ownership only, detail-only. */}
              <Stamp tone="copper" owned={needsViewerAction} />
              {client.phone && (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-2 border-[var(--line-control)]"
                    aria-label="WhatsApp"
                    onClick={() => {
                      const phone = client.phone?.replace(/\D/g, "");
                      if (phone)
                        window.open(
                          `https://wa.me/${phone.startsWith("0") ? "62" + phone.slice(1) : phone}`,
                          "_blank",
                        );
                    }}
                  >
                    <MessageCircle className="w-4 h-4 text-[var(--accent-whatsapp)]" />
                    <span className="hidden sm:inline">WhatsApp</span>
                  </Button>
                </>
              )}
              {client.email && (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-2 border-[var(--line-control)]"
                  aria-label="Email"
                  onClick={() =>
                    window.open(`mailto:${client.email}`, "_blank")
                  }
                >
                  <Mail className="w-4 h-4" />
                  <span className="hidden sm:inline">Email</span>
                </Button>
              )}
              {client.google_drive_folder_id && (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-2 text-[var(--tx-secondary)] border-[var(--bz-border)] hover:bg-[var(--bz-surface)] hover:text-[var(--tx-pure)]"
                  onClick={() =>
                    window.open(
                      `https://drive.google.com/drive/folders/${client.google_drive_folder_id}`,
                      "_blank",
                    )
                  }
                  title="Open client's Google Drive folder"
                  aria-label="Open client's Google Drive folder"
                >
                  <FolderOpen className="w-4 h-4" />
                  <span className="hidden sm:inline">Drive</span>
                </Button>
              )}
            </>
          }
        />
      </div>

      {/*
       * "Where it stands" — status pill, assignment, key dates, the alert
       * strip — FIRST in the DOM at every width (concept.md C14b / K3b spec
       * §3.2), moved to the right column above 1100px by
       * `styles.statusColumn`'s own `order`, never by giving the main
       * column `order: -1` (see the CSS module's own note).
       */}
      <div className={styles.detailLayout}>
        <div className={styles.statusColumn} data-testid="status-column">
          {/* Status pill — a menu TRIGGER; see the STATUS_TRIGGER_BASE note
              above for why it stays a native button styled from PILL_TONE
              instead of the StatePill component. Its options ARE StatePill. */}
          <div ref={statusMenuRef} className="relative inline-block">
            <button
              type="button"
              onClick={() => setShowStatusMenu((v) => !v)}
              disabled={isUpdatingStatus}
              className={`${STATUS_TRIGGER_BASE} ${PILL_TONE[clientTone]}`}
              title="Click to change status"
              aria-label="Change client status"
              aria-haspopup="menu"
              aria-expanded={showStatusMenu}
            >
              {isUpdatingStatus ? "..." : client.status}
            </button>
            {showStatusMenu && (
              <div className="absolute top-full left-0 mt-1 z-50 flex flex-col gap-1 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-surface)] shadow-xl p-1 min-w-[140px]">
                {(
                  ["lead", "active", "completed", "lost", "inactive"] as const
                ).map((s) => (
                  <StatePill
                    key={s}
                    tone={clientStatusTone(s)}
                    label={s}
                    pressed={s === client.status}
                    onClick={() => updateStatus(s)}
                  />
                ))}
              </div>
            )}
          </div>

          <p className="text-sm text-[var(--bz-text-2)]">
            {client.client_type || "Individual"}
            {isMounted &&
              client.last_interaction_date &&
              (() => {
                const days = Math.floor(
                  (Date.now() -
                    new Date(client.last_interaction_date).getTime()) /
                    86400000,
                );
                if (days > 30)
                  return (
                    <span className="text-[var(--state-warning)]">
                      {" "}
                      • Silent {days}d
                    </span>
                  );
                if (days > 14)
                  return (
                    <span className="text-[var(--state-warning)]">
                      {" "}
                      • {days}d ago
                    </span>
                  );
                return null;
              })()}
          </p>

          {/* Team — who holds this client: "Setup" is the owner (assigned_to);
              "Tax consultant" appears for a tax client (a consultant on record
              or LKPM history) and stays changeable in place. People are named
              by first name only. */}
          {(client.assigned_to || isTaxClient) && (
            <div
              className="overflow-hidden rounded-xl border border-[var(--bz-border)] bg-[var(--bz-card)]"
              data-testid="client-team"
            >
              {client.assigned_to && (
                <div
                  className="flex items-center gap-3 px-3 py-2.5"
                  data-testid="team-setup"
                >
                  <AvatarWithFallback
                    src={setupOption?.avatar}
                    alt={setupName}
                    className="h-8 w-8 rounded-full object-cover"
                    fallback={
                      <div className="flex h-8 w-8 items-center justify-center rounded-full border border-[var(--bz-border)] bg-[var(--bz-base)] text-[11px] font-semibold text-[var(--tx-pure)]">
                        {initials(setupName)}
                      </div>
                    }
                  />
                  <div className="flex min-w-0 flex-col">
                    <span className="text-[11px] text-[var(--tx-secondary)]">
                      Setup
                    </span>
                    <span
                      className="truncate text-sm font-medium text-[var(--tx-pure)]"
                      data-testid="team-setup-name"
                    >
                      {setupName}
                    </span>
                  </div>
                </div>
              )}
              {isTaxClient && (
                <div
                  className={`flex items-center gap-3 px-3 py-2.5 ${client.assigned_to ? "border-t border-[var(--bz-border)]" : ""}`}
                  data-testid="team-tax-consultant"
                >
                  <AvatarWithFallback
                    src={
                      teamMemberOptions.find(
                        (m) => m.value === taxConsultantValue,
                      )?.avatar
                    }
                    alt={taxConsultantLabel || "Tax consultant"}
                    className="h-8 w-8 rounded-full object-cover"
                    fallback={
                      <div className="flex h-8 w-8 items-center justify-center rounded-full border border-dashed border-[var(--line-control)] bg-[var(--bz-base)] text-[11px] font-semibold text-[var(--tx-pure)]">
                        {taxConsultantLabel
                          ? initials(taxConsultantLabel)
                          : "—"}
                      </div>
                    }
                  />
                  <div className="flex min-w-0 flex-1 flex-col">
                    <label
                      htmlFor="tax-consultant-team"
                      className="text-[11px] text-[var(--tx-secondary)]"
                    >
                      Tax consultant
                    </label>
                    <select
                      id="tax-consultant-team"
                      value={taxConsultantValue}
                      onChange={(e) => void saveTaxConsultant(e.target.value)}
                      disabled={isSavingTaxConsultant}
                      className={`-ml-0.5 cursor-pointer appearance-auto truncate rounded bg-transparent text-sm font-medium text-[var(--tx-pure)] ${FOCUS}`}
                    >
                      <option value="">Not assigned</option>
                      {taxConsultantValue &&
                        !taxConsultants.some(
                          (c) => c.value === taxConsultantValue,
                        ) && (
                          <option value={taxConsultantValue} disabled>
                            {taxConsultantLabel}
                          </option>
                        )}
                      {taxConsultants.map((c) => (
                        <option key={c.value} value={c.value}>
                          {firstName(c.label, c.value)}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Counters — the backend's own attention_stats, expired and urgent
              only. Absent stats = no counters (the old view counts were
              noise). A count is urgency (a date), never ownership: warning,
              never danger, and always a word next to the number. */}
          {attentionStats &&
            (attentionStats.expired > 0 || attentionStats.urgent > 0) && (
              <div
                className="flex flex-wrap gap-2"
                data-testid="attention-counters"
              >
                {attentionStats.expired > 0 && (
                  <span className="flex items-center gap-1 rounded-full border border-[var(--state-warning)] px-2 py-1 text-xs font-bold text-[var(--state-warning)]">
                    <AlertCircle className="h-3 w-3" aria-hidden="true" />
                    {attentionStats.expired} expired
                  </span>
                )}
                {attentionStats.urgent > 0 && (
                  <span className="flex items-center gap-1 rounded-full border border-[var(--line-control)] px-2 py-1 text-xs font-semibold text-[var(--state-warning)]">
                    <Bell className="h-3 w-3" aria-hidden="true" />
                    {attentionStats.urgent} urgent
                  </span>
                )}
              </div>
            )}
        </div>

        <div className={styles.mainColumn}>
          {/* Tabs — hairline idiom: 44px row, --line-control underline, an ink
          underline on the active tab. No role="tab"/aria-selected here: K3a
          dropped role="menu" for the same reason (decision D6) — this bar
          does not implement the arrow-key pattern a tab role promises, so it
          keeps native buttons with the browser's own Tab/Enter/Space. The row
          is wrapped in a `<nav>` with its own accessible name and the active
          button carries `aria-current="page"`. Order (owner, 2026-11-03):
          overview, practices (`process`), immigration (`visas`), company, tax,
          family, documents. There is no Activity tab: `?tab=timeline` and
          `?tab=whatsapp` are not TAB_KEYS any more, so they open Overview. */}
          <nav
            ref={tabsRef}
            className={styles.tabBar}
            data-testid="tab-bar"
            aria-label="Client sections"
          >
            {[
              { key: "overview", label: "Overview", icon: User },
              {
                key: "process",
                label: `Practices (${activePractices.length + completedPractices.length})`,
                icon: FolderOpen,
              },
              { key: "visas", label: "Immigration", icon: Globe },
              { key: "company", label: "Company", icon: Building2 },
              { key: "tax", label: "Tax", icon: DollarSign },
              {
                key: "family",
                label: `Family (${stats.family_count})`,
                icon: Users,
              },
              {
                key: "documents",
                label: `Documents (${documentRows.length})`,
                icon: FileText,
              },
            ]
              .filter(({ key }) => {
                if (key === "company") return showCompanyTab;
                if (key === "tax") return showTaxTab;
                return true;
              })
              .map(({ key, label, icon: Icon }) => {
                const isActive = visibleTab === key;
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => {
                      if (key === "documents") setDocumentsGroup("all");
                      handleTabChange(key as TabType, isActive);
                    }}
                    // Fetch the tab's chunk a moment before the click.
                    onPointerEnter={() => void TAB_LOADERS[key as TabType]?.()}
                    onFocus={() => void TAB_LOADERS[key as TabType]?.()}
                    aria-current={isActive ? "page" : undefined}
                    className={`${styles.tab} ${isActive ? styles.tabActive : ""}`}
                  >
                    <Icon className="w-4 h-4" />
                    {label}
                  </button>
                );
              })}
          </nav>

          {/* Tab Content */}
          {visibleTab === "overview" && (
            <>
              <OverviewTab
                client={client}
                stats={stats}
                documents={documents}
                activePractices={activePractices}
                completedPractices={completedPractices}
                attention={profile.attention}
                attentionError={profile.attention_error}
                needsViewerAction={needsViewerAction}
                formatDate={formatDate}
                formatCurrency={formatCurrency}
                onEditClick={() => setActiveModal("edit_client")}
                onDeleteClick={confirmDeleteClient}
                canDeleteClient={canDeleteClient}
                isDeletingClient={isDeletingClient}
                onRefresh={invalidateClient}
                clientId={clientId}
                familyMembers={family_members}
                companyLinks={company_links ?? []}
                interactions={interactions}
                viewerEmail={currentUserEmail}
                onOpenTab={(tab) => handleTabChange(tab)}
                onOpenRemoved={openRemovedDocuments}
                day={profileDay!}
              />
              {businessStoryShown && (
                <BusinessStoryPanel
                  n={overviewLedgerN + 1}
                  clientName={client.full_name}
                  companyNames={businessStoryCompanyNames}
                  maps={businessStoryQuery.data ?? []}
                  isLoading={businessStoryQuery.isLoading}
                  error={businessStoryError}
                />
              )}
              {/* R7a: for a client with no company links (and no company name
                  on record) the Company/Tax tabs are hidden — so the actions
                  they carried move here, to the Overview the deep links fall
                  back to: linking the first company, and — only when the Tax
                  tab is hidden too — the tax-consultant assignment TaxTab
                  renders (two live copies of one control on one page would be
                  a defect). An r19 LedgerSection, not a boxed card: the Add
                  company control sits in the always-visible actions slot,
                  never in a hover-only row action. */}
              {!showCompanyTab && (
                <LedgerSection
                  n={overviewLedgerN + (businessStoryShown ? 2 : 1)}
                  tone="wait"
                  title="Company & tax"
                  actions={
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setIsAddingCompany(true)}
                    >
                      Add company
                    </Button>
                  }
                >
                  <EmptyState>No company linked yet.</EmptyState>
                  {!showTaxTab && !isTaxClient && (
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3">
                      <label
                        htmlFor="tax-consultant-inline"
                        className={EYEBROW}
                      >
                        Tax consultant
                      </label>
                      <select
                        id="tax-consultant-inline"
                        value={taxConsultantValue}
                        onChange={(e) => void saveTaxConsultant(e.target.value)}
                        disabled={isSavingTaxConsultant}
                        className={
                          FIELD +
                          " max-w-[260px] cursor-pointer appearance-auto"
                        }
                      >
                        <option value="">— not assigned —</option>
                        {taxConsultantValue &&
                          !taxConsultants.some(
                            (c) => c.value === taxConsultantValue,
                          ) && (
                            <option value={taxConsultantValue} disabled>
                              {legacyConsultantLabel(taxConsultantValue)}
                            </option>
                          )}
                        {taxConsultants.map((c) => (
                          <option key={c.value} value={c.value}>
                            {c.label}
                          </option>
                        ))}
                      </select>
                      {isSavingTaxConsultant && (
                        <span className="text-xs text-[var(--tx-secondary)]">
                          Saving…
                        </span>
                      )}
                    </div>
                  )}
                </LedgerSection>
              )}
            </>
          )}

          {activeTab === "documents" && passportDocs.length > 0 && (
            <Notice
              tone="wait"
              role="status"
              className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1"
            >
              <span>
                {passportDocs.length === 1
                  ? "1 passport document is shown in Overview › Passport"
                  : `${passportDocs.length} passport documents are shown in Overview › Passport`}
              </span>
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0"
                onClick={() => {
                  setScrollToPassport(true);
                  handleTabChange("overview");
                }}
              >
                Open Passport
              </Button>
            </Notice>
          )}
          {activeTab === "documents" && (
            <DocumentsTab
              clientId={clientId}
              key={documentsGroup}
              initialGroup={documentsGroup}
              day={profileDay!}
              documents={documentRows}
              documentsByCategory={documentsByCategory}
              formatDate={formatDate}
              onAddClick={() => setActiveModal("add_document")}
              onEditClick={(doc) => {
                setEditingDocument(doc);
                setActiveModal("edit_document");
              }}
              onRefresh={invalidateClient}
            />
          )}
          {activeTab === "documents" && client.google_drive_folder_id && (
            <FilesBrowserPanel folderId={client.google_drive_folder_id} />
          )}

          {activeTab === "process" && (
            <ProcessTab
              day={profileDay!}
              clientId={clientId}
              practices={[...activePractices, ...completedPractices]}
              formatDate={formatDate}
              onRefresh={invalidateClient}
            />
          )}

          {activeTab === "family" && (
            <FamilyTab
              clientId={clientId}
              familyMembers={family_members}
              documents={documents}
              formatDate={formatDate}
              onAddClick={() => setActiveModal("add_family")}
              onEditClick={(member) => {
                setEditingFamilyMember(member);
                setActiveModal("edit_family");
              }}
              onRefresh={invalidateClient}
              onOpenRemoved={openRemovedDocuments}
              day={profileDay!}
            />
          )}

          {activeTab === "visas" && (
            <ImmigrationTab
              clientId={clientId}
              documents={documents}
              formatDate={formatDate}
              onAddClick={() => setActiveModal("add_document")}
              onEditClick={(doc) => {
                setEditingDocument(doc);
                setActiveModal("edit_document");
              }}
              onRefresh={invalidateClient}
              onOpenFamilyTab={() => handleTabChange("family")}
              onOpenRemoved={openRemovedDocuments}
              day={profileDay!}
            />
          )}

          {visibleTab === "company" && (
            <CompanyTab
              clientId={clientId}
              client={client}
              documents={documents}
              formatDate={formatDate}
              onRefresh={invalidateClient}
              onOpenDocuments={() => handleTabChange("documents")}
              onOpenRemoved={openRemovedDocuments}
              companyLinks={company_links}
            />
          )}

          {visibleTab === "tax" && (
            <TaxTab
              clientId={clientId}
              formatDate={formatDate}
              client={profile?.client ?? null}
              companyLinks={company_links}
              onRefresh={invalidateClient}
              taxConsultants={taxConsultants}
              documents={taxDocuments}
              attention={profile?.attention}
              day={profileDay!}
            />
          )}
        </div>
      </div>

      {/* Modals */}
      {activeModal === "edit_client" && profile && (
        <EditClientModal
          client={profile.client}
          onClose={() => setActiveModal("none")}
          onSave={invalidateClient}
        />
      )}

      {activeModal === "add_family" && (
        <AddFamilyMemberModal
          clientId={clientId}
          onClose={() => setActiveModal("none")}
          onSave={invalidateClient}
        />
      )}

      {activeModal === "edit_family" && editingFamilyMember && (
        <EditFamilyMemberModal
          clientId={clientId}
          member={editingFamilyMember}
          onClose={() => {
            setActiveModal("none");
            setEditingFamilyMember(null);
          }}
          onSave={invalidateClient}
        />
      )}

      {activeModal === "add_document" && (
        <AddDocumentModal
          clientId={clientId}
          categories={docCategories}
          familyMembers={family_members}
          clientHasDriveFolder={!!client.google_drive_folder_id}
          onClose={() => setActiveModal("none")}
          onSave={invalidateClient}
        />
      )}

      {activeModal === "edit_document" && editingDocument && (
        <EditDocumentModal
          clientId={clientId}
          document={editingDocument}
          categories={docCategories}
          familyMembers={family_members}
          canChangeVisibility={viewerCanChangeDocumentVisibility(
            currentUserRole,
          )}
          onClose={() => {
            setActiveModal("none");
            setEditingDocument(null);
          }}
          onSave={invalidateClient}
        />
      )}

      {isAddingCompany && (
        <AddCompanyModal
          clientId={clientId}
          onClose={() => setIsAddingCompany(false)}
          onSuccess={() => {
            setIsAddingCompany(false);
            void invalidateClient();
          }}
        />
      )}
    </div>
  );
}

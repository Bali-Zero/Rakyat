"use client";

import React, { useState, useEffect } from "react";
import Image from "next/image";
import Link from "next/link";
import { ArrowUpRight, Edit2, Loader2, Plus, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { logger } from "@/lib/logger";
import { isLive } from "./documentValidity";
import { RemovedSignpost } from "./RemovedSignpost";
import type { KbliResolution } from "@/lib/kbli-resolve";
import type {
  ClientCompanyLink,
  ClientProfile,
  ClientDocument,
  CompanyDocument,
} from "@/lib/api/crm/crm.types";
import {
  CellStack,
  EmptyState,
  EYEBROW,
  FOCUS,
  HairlineBody,
  HairlineGrid,
  HairlineHead,
  HairlineRow,
  LedgerSection,
  Numeral,
  SERIF_SECTION,
  StatePill,
  TABULAR,
  type PillTone,
} from "@/components/workspace/r19";
import {
  companyTypeSubtitles,
  computeAge,
  formatCapital,
  formatCapitalFull,
  getInitials,
} from "@/components/portal/company/editorial-tokens";
import {
  COMPANY_TYPE_OPTIONS,
  normalizeCompanyType,
} from "./company/companyType";
import { CompanyDocUpload } from "./company/CompanyDocUpload";
import { EditCompanyModal } from "./company/EditCompanyModal";
import { AddCompanyModal } from "./modals/AddCompanyModal";
import { CompanyIdentity } from "./company/CompanyIdentity";
import {
  clearCompanyDetailCache,
  fetchCompanyDetail,
} from "./company/companyDetailCache";
import { AiRecap } from "./AiRecap";
import { LegalTimeline, type LegalMilestone } from "./company/LegalTimeline";
import {
  groupAssociates,
  personClientId,
  type Associate,
} from "./company/associates";

/** The person's name is the primary text of its row: kita heading face
 * (SERIF_SECTION), 15px, 16px from sm up, weight 600 — a weight the kita
 * heading face actually ships, so the browser never synthesizes it. */
const PERSON_NAME =
  "min-w-0 truncate text-[15px] leading-tight sm:text-[16px] underline-offset-4 group-hover/person:underline";
const PERSON_NAME_STYLE = { ...SERIF_SECTION, fontWeight: 600 };

/** Below sm a shareholder row stacks: the meta cells read "Role x", "Shares n", "Ownership p%". */
const META_LABEL =
  "max-sm:text-[12px] max-sm:before:text-[var(--tx-secondary)]";

const DOC_VAULT_SLOTS = [
  { docType: "akta_pendirian", label: "Akta Pendirian", hint: "PDF/JPG" },
  { docType: "sk_decree", label: "SK Kemenkumham", hint: "PDF/JPG" },
  { docType: "npwp", label: "NPWP Perusahaan", hint: "PDF/JPG" },
  { docType: "nib", label: "NIB", hint: "PDF/JPG" },
  { docType: "company_profile", label: "Company Profile", hint: "PDF" },
  { docType: "wlkp", label: "WLKP", hint: "PDF" },
  { docType: "bpjs", label: "BPJS Ketenagakerjaan", hint: "PDF" },
  { docType: "organogram", label: "Bagan Organisasi", hint: "PDF/JPG" },
  { docType: "rekening_koran", label: "Rekening Koran", hint: "PDF" },
];

/** Real `company_status` word for every value the field can carry — the old
 * chip only covered active/in_setup/dormant and rendered NOTHING for
 * "dissolved" (a gap, not a design choice); this closes it with the same
 * field, never a guess. */
function companyStatusPill(status: string): { tone: PillTone; label: string } {
  if (status === "active") return { tone: "ok", label: "Active" };
  if (status === "in_setup") return { tone: "wait", label: "In Setup" };
  if (status === "dormant") return { tone: "wait", label: "Dormant" };
  if (status === "dissolved") return { tone: "wait", label: "Dissolved" };
  return { tone: "wait", label: status };
}

/** Same `custom_fields` shape three old sub-components each parsed on their
 * own (authorized capital, shareholder count, OCR shareholder list) —
 * consolidated into one parse, same try/catch-swallow behaviour. */
function parseCustomFields(
  raw: Record<string, unknown> | string | undefined,
): Record<string, unknown> {
  try {
    const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
    return parsed && typeof parsed === "object"
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

/** Ownership % is a share of the COMPANY: the record's `shares_count` is the
 * denominator whenever it exists, and the sum of the OCR'd rows only stands
 * in when it does not — same priority the old `PeopleColumn` applied. */
function parseOcrShareholders(
  cf: Record<string, unknown>,
  totalShares?: number,
): Array<{ name?: string; role: string; shares?: number; pct?: number }> {
  try {
    const sh = cf.shareholders;
    if (!sh) return [];
    const parsed = typeof sh === "string" ? JSON.parse(sh) : sh;
    if (!Array.isArray(parsed) || parsed.length === 0) return [];
    const total =
      totalShares ||
      parsed.reduce(
        (sum: number, s: { shares?: number }) => sum + (s.shares || 0),
        0,
      );
    return parsed.map(
      (s: { name?: string; role?: string; shares?: number }) => ({
        name: s.name,
        role: s.role?.toLowerCase() || "shareholder",
        shares: s.shares,
        pct:
          total > 0 && s.shares
            ? Math.round((s.shares / total) * 100 * 10) / 10
            : undefined,
      }),
    );
  } catch {
    return [];
  }
}

function KvItem({
  label,
  value,
  sub,
  action,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className={EYEBROW}>{label}</span>
      <span className="flex items-center gap-1.5 text-[15px] font-semibold text-[var(--tx-pure)]">
        <span style={TABULAR}>{value}</span>
        {action}
      </span>
      {sub ? (
        <span className="text-[12px] text-[var(--tx-secondary)]">{sub}</span>
      ) : null}
    </div>
  );
}

const KV_GRID =
  "grid grid-cols-1 gap-x-8 gap-y-5 py-5 sm:grid-cols-2 lg:grid-cols-3";

// ============================================
// COMPANY TAB (main export)
// ============================================
export function CompanyTab({
  clientId,
  client,
  documents,
  formatDate,
  onRefresh,
  onOpenDocuments,
  onOpenRemoved,
  companyLinks,
}: {
  clientId: number;
  client: ClientProfile["client"];
  documents: ClientDocument[];
  formatDate: (d: string) => string;
  onRefresh: () => Promise<void>;
  /** Switches the client card to its Documents tab. */
  onOpenDocuments?: () => void;
  /** Opens the Documents "Removed or rejected" group. */
  onOpenRemoved?: () => void;
  /** The client's links from the profile already on screen: the identity
   * renders from them with no request of its own. */
  companyLinks?: ClientCompanyLink[];
}) {
  // Holding count (row 33): only a live document is on file. A removed,
  // archived or rejected one is listed once, in Documents, and signposted.
  const pmaDocs = documents.filter(
    (d) => d.document_category === "pma" && isLive(d),
  );
  const voidPmaDocs = documents.filter(
    (d) => d.document_category === "pma" && !isLive(d),
  ).length;

  const [companyData, setCompanyData] = useState<{
    company_name: string;
    company_type: string;
    kbli_code?: string;
    kbli_description?: string;
    nib?: string;
    npwp_company?: string;
    akta_pendirian_no?: string;
    akta_pendirian_date?: string;
    akta_perubahan_no?: string;
    akta_perubahan_date?: string;
    sk_menhumkam_no?: string;
    sk_menhumkam_date?: string;
    registered_address?: string;
    office_address?: string;
    city?: string;
    province?: string;
    company_status?: string;
    shares_count?: number;
    share_nominal_value?: number;
    setup_progress?: number;
    company_id?: number;
    custom_fields?: Record<string, unknown>;
  } | null>(null);

  const [associates, setAssociates] = useState<Associate[]>([]);

  // Links beyond the one shown above. A client can hold several active PT
  // links; listing them keeps a second PT from being silently hidden.
  const [otherLinks, setOtherLinks] = useState<
    Array<{ link_id: number; company_name: string; role: string }>
  >([]);

  const [companyDocs, setCompanyDocs] = useState<CompanyDocument[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isEditingCompany, setIsEditingCompany] = useState(false);
  const [isAddingCompany, setIsAddingCompany] = useState(false);
  const [isSyncingDrive, setIsSyncingDrive] = useState(false);
  const [reloadTrigger, setReloadTrigger] = useState(0);
  // Every mutation made from this tab (edit, Sync Drive, Add company, a vault
  // upload) goes through here: the cached detail is dropped first, so the
  // next open of the tab never shows a stale copy of the staff's own change.
  const reload = () => {
    clearCompanyDetailCache();
    setReloadTrigger((t) => t + 1);
  };
  // The navigator is the source of truth for KBLI titles: the server resolves
  // each stored code against the same dataset /kbli/[code] is built from.
  // `null` until it answers, so no row links to a page that may not exist.
  const [kbliResolved, setKbliResolved] = useState<Record<
    string,
    KbliResolution
  > | null>(null);
  const [legacyEnglish, setLegacyEnglish] = useState<Record<string, string>>(
    {},
  );
  const kbliCodeList = companyData?.kbli_code;
  useEffect(() => {
    if (!kbliCodeList) return;
    let cancelled = false;
    const codes = kbliCodeList
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean);
    void fetch(`/api/kbli/resolve?codes=${encodeURIComponent(codes.join(","))}`)
      .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
      .then(async (body: { resolutions: Record<string, KbliResolution> }) => {
        const stale = codes.filter((c) => !body.resolutions[c]?.inNavigator);
        if (stale.length > 0) {
          const legacy = await import("@/lib/kbli-legacy-english");
          const next: Record<string, string> = {};
          for (const c of stale) {
            const t = legacy.LEGACY_ENGLISH_TITLES[c];
            if (t) next[c] = t;
          }
          if (!cancelled) setLegacyEnglish(next);
        }
        if (!cancelled) setKbliResolved(body.resolutions);
      })
      .catch(() => {
        // Titles stay as recorded, unlinked.
      });
    return () => {
      cancelled = true;
    };
  }, [kbliCodeList]);

  useEffect(() => {
    let cancelled = false;

    async function loadCompanyData() {
      try {
        // Step 1: Try linked companies
        // The profile already carries the links: no round trip for them on the
        // first load (a reload after an edit asks the server again).
        const linked =
          reloadTrigger === 0 && companyLinks?.length
            ? companyLinks
            : await api.crm.getClientCompanies(clientId);
        // The flagged primary link wins; the first row is only the fallback
        // when no link carries is_primary (the old linked[0] behaviour).
        const primary = linked.find((l) => l.is_primary) ?? linked[0];
        if (!cancelled) {
          setOtherLinks(
            linked
              .filter((l) => l !== primary)
              .map((l) => ({
                link_id: l.link_id,
                company_name: l.company_name,
                role: l.role,
              })),
          );
        }
        if (!cancelled && primary) {
          const co = primary;
          setCompanyData({
            company_name: co.company_name,
            company_type: co.company_type,
            kbli_code: co.kbli_code,
            kbli_description: co.kbli_description,
            nib: co.nib,
            npwp_company: co.npwp_company,
            akta_pendirian_no: co.akta_pendirian_no,
            akta_pendirian_date: co.akta_pendirian_date,
            akta_perubahan_no: co.akta_perubahan_no,
            akta_perubahan_date: co.akta_perubahan_date,
            sk_menhumkam_no: co.sk_menhumkam_no,
            sk_menhumkam_date: co.sk_menhumkam_date,
            registered_address: co.registered_address,
            office_address: co.office_address,
            city: co.city,
            province: co.province,
            company_status: co.company_status,
            shares_count: co.shares_count,
            share_nominal_value: co.share_nominal_value,
            setup_progress: co.setup_progress,
            company_id: co.company_id,
            custom_fields: co.custom_fields,
          });

          if (co.company_id) {
            fetchCompanyDetail(co.company_id, reloadTrigger > 0)
              .then((full) => {
                if (cancelled) return;
                if (full.associates?.length) {
                  const groupedAssociates = groupAssociates(full.associates);
                  setAssociates(groupedAssociates);
                  const totalShares = groupedAssociates.reduce(
                    (sum, a) => sum + (a.shares_count || 0),
                    0,
                  );
                  if (totalShares > 0) {
                    setCompanyData((prev) =>
                      prev ? { ...prev, shares_count: totalShares } : prev,
                    );
                  }
                } else {
                  setAssociates([
                    {
                      client_name: client.full_name,
                      role: co.role,
                      ownership_percentage: co.ownership_percentage,
                      shares_count: co.shares_count,
                    },
                  ]);
                }
                if (full.documents?.length) setCompanyDocs(full.documents);
              })
              .catch((err) => {
                logger.error(
                  "[CompanyTab] Failed to load company details",
                  {},
                  err instanceof Error ? err : new Error(String(err)),
                );
                setAssociates([
                  {
                    client_name: client.full_name,
                    role: co.role,
                    ownership_percentage: co.ownership_percentage,
                    shares_count: co.shares_count,
                  },
                ]);
              });
          } else {
            setAssociates([
              {
                client_name: client.full_name,
                role: co.role,
                ownership_percentage: co.ownership_percentage,
                shares_count: co.shares_count,
              },
            ]);
          }
          return;
        }

        // Step 2: Fallback — search by client.company_name
        if (client.company_name) {
          const found = await api.crm.searchCompanyByName(client.company_name);
          if (!cancelled && found) {
            setCompanyData({
              company_name: found.company_name,
              company_type: found.company_type,
              kbli_code: found.kbli_code,
              kbli_description: found.kbli_description,
              nib: found.nib,
              npwp_company: found.npwp_company,
              akta_pendirian_no: found.akta_pendirian_no,
              akta_pendirian_date: found.akta_pendirian_date,
              akta_perubahan_no: found.akta_perubahan_no,
              akta_perubahan_date: found.akta_perubahan_date,
              sk_menhumkam_no: found.sk_menhumkam_no,
              sk_menhumkam_date: found.sk_menhumkam_date,
              registered_address: found.registered_address,
              office_address: found.office_address,
              city: found.city,
              province: found.province,
              company_status: found.status,
              company_id: found.id,
              custom_fields: found.custom_fields,
            });
            if (found.associates?.length) {
              const groupedAssociates2 = groupAssociates(found.associates);
              setAssociates(groupedAssociates2);
              const totalShares2 = groupedAssociates2.reduce(
                (sum, a) => sum + (a.shares_count || 0),
                0,
              );
              if (totalShares2 > 0) {
                setCompanyData((prev) =>
                  prev ? { ...prev, shares_count: totalShares2 } : prev,
                );
              }
            }
            api.crm
              .getCompanyDocuments(found.id)
              .then((docs) => !cancelled && setCompanyDocs(docs))
              .catch((err) => {
                logger.error(
                  "[CompanyTab] Failed to load company documents",
                  {},
                  err instanceof Error ? err : new Error(String(err)),
                );
              });
            return;
          }
        }
      } catch (err) {
        if (!cancelled) {
          toast.error("Failed to load company data", {
            description: (err as Error).message,
          });
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }

    loadCompanyData();
    return () => {
      cancelled = true;
    };
    // companyLinks is read once per load on purpose (a new array identity on
    // every parent render must not refetch).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId, client.company_name, client.full_name, reloadTrigger]);

  // ── LOADING ────────────────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-6 h-6 animate-spin text-[var(--tx-secondary)]" />
      </div>
    );
  }

  // ── EMPTY STATE ────────────────────────────────────────────────────────
  const hasCompanyName = !!client.company_name;
  const hasAnyDoc = pmaDocs.length > 0;

  if (!companyData && !hasCompanyName && !hasAnyDoc) {
    return (
      <>
        <EmptyState
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-2"
              onClick={() => setIsAddingCompany(true)}
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Add company
            </Button>
          }
        >
          No company linked yet.
        </EmptyState>
        {isAddingCompany && (
          <AddCompanyModal
            clientId={clientId}
            onClose={() => setIsAddingCompany(false)}
            onSuccess={() => {
              setIsAddingCompany(false);
              reload();
              void onRefresh();
            }}
          />
        )}
      </>
    );
  }

  // ── DERIVED DATA ───────────────────────────────────────────────────────
  const co = companyData;
  const companyName = co?.company_name || client.company_name || "Company";
  const companyTypeRaw = co?.company_type || "";
  const normalizedType = normalizeCompanyType(companyTypeRaw);
  const companyTypeOption = COMPANY_TYPE_OPTIONS.find(
    (o) => o.value === normalizedType,
  );
  const companyTypeLabel = companyTypeRaw
    ? companyTypeOption?.label || companyTypeRaw
    : "—";
  // Indonesian expansion of the entity type, keyed on the stored value first
  // and on its canonical form second, so a legacy `PT_PMA` row keeps its gloss.
  const companyTypeSubtitle = companyTypeRaw
    ? companyTypeSubtitles[companyTypeRaw] ||
      companyTypeSubtitles[normalizedType]
    : undefined;
  const isPMA = companyTypeRaw === "PT PMA" || companyTypeRaw === "PMA";
  const capital = formatCapital(co?.shares_count, co?.share_nominal_value);

  const customFields = parseCustomFields(co?.custom_fields);
  const capitalFull =
    formatCapitalFull(co?.shares_count, co?.share_nominal_value) ||
    (() => {
      const authCap = customFields.authorized_capital;
      if (authCap) {
        const num = Number(authCap);
        if (!isNaN(num) && num > 0) return `Rp ${num.toLocaleString("id-ID")}`;
      }
      return null;
    })();

  const ocrShareholders = parseOcrShareholders(customFields, co?.shares_count);
  const people =
    ocrShareholders.length > associates.length
      ? ocrShareholders.map((s) => ({
          client_id: undefined as number | undefined,
          name: s.name,
          role: s.role,
          shares: s.shares,
          pct: s.pct,
        }))
      : associates.map((a) => ({
          client_id: a.client_id,
          name: a.client_name,
          role: a.role,
          shares: a.shares_count,
          pct: a.ownership_percentage,
        }));
  const shareholderCount = people.length;

  // If pendirian fields are identical to perubahan, treat pendirian as absent
  const pendirianIsDuplicate =
    co?.akta_pendirian_no &&
    co?.akta_pendirian_no === co?.akta_perubahan_no &&
    co?.akta_pendirian_date === co?.akta_perubahan_date;
  const effectivePendirianNo = pendirianIsDuplicate
    ? undefined
    : co?.akta_pendirian_no;
  const effectivePendirianDate = pendirianIsDuplicate
    ? undefined
    : co?.akta_pendirian_date;

  const foundingDate = effectivePendirianDate || co?.sk_menhumkam_date;
  const age = computeAge(foundingDate);

  const streetAddress = co?.registered_address || co?.office_address;
  const cityProvince = [co?.city, co?.province].filter(Boolean).join(", ");

  // Merge docs for count
  const allDocs = [
    ...pmaDocs.map((d) => ({
      key: `client-${d.id}`,
      name: d.file_name || d.document_type,
    })),
    ...companyDocs
      .filter((cd) => cd.google_drive_file_url || cd.google_drive_file_id)
      .map((cd) => ({
        key: `company-${cd.id}`,
        name: cd.file_name || cd.document_type,
      })),
  ];

  const hasLegalData =
    effectivePendirianNo || co?.akta_perubahan_no || co?.sk_menhumkam_no;

  const hasCapitalFacts =
    capitalFull || co?.shares_count || co?.akta_perubahan_no;

  const kbliCodes = co?.kbli_code
    ? co.kbli_code.split(",").map((c) => c.trim())
    : [];
  const kbliDescriptions = (co?.kbli_description || "")
    .split(";")
    .map((d) => d.trim());

  // Legal timeline milestones. The timeline orders them by date (newest
  // first, undated ones after); this list only says what exists.
  const legalEntries: LegalMilestone[] = [];
  if (co?.akta_perubahan_no && co?.akta_perubahan_date) {
    legalEntries.push({
      key: "amendment",
      kind: "Amendment deed",
      title: `Akta Perubahan · Revision #${co.akta_perubahan_no}`,
      body: `Amendment filed with Kemenkumham.${capital ? ` Authorized capital updated to ${capital}.` : ""}`,
      refText: `Akta Perubahan #${co.akta_perubahan_no}`,
      date: co.akta_perubahan_date,
    });
  }
  if (co?.nib) {
    legalEntries.push({
      key: "nib",
      kind: "NIB",
      title: "NIB Issued & OSS Platform Verified",
      body: "Registered on the Online Single Submission (OSS) platform.",
      refText: `NIB ${co.nib}`,
    });
  }
  if (co?.npwp_company) {
    legalEntries.push({
      key: "npwp",
      kind: "NPWP",
      title: "Company tax number registered",
      body: "Tax identification number on record.",
      refText: `NPWP ${co.npwp_company}`,
    });
  }
  if (effectivePendirianNo && effectivePendirianDate) {
    legalEntries.push({
      key: "incorporation",
      kind: "Deed of establishment",
      title: `${companyName} Established`,
      body: `Incorporated as a Perseroan Terbatas${isPMA ? " under the PMA regime (foreign direct investment)" : ""}.`,
      refText: `Akta Pendirian #${effectivePendirianNo}`,
      date: effectivePendirianDate,
    });
    if (co?.sk_menhumkam_no) {
      legalEntries.push({
        key: "sk",
        kind: "SK Menkumham",
        title: "Approved by the Ministry of Law and Human Rights",
        body: "Ministerial decree approving the deed of establishment.",
        refText: co.sk_menhumkam_no,
        date: co.sk_menhumkam_date,
      });
    }
  } else if (co?.sk_menhumkam_no && co?.sk_menhumkam_date) {
    legalEntries.push({
      key: "incorporation-sk",
      kind: "SK Menkumham",
      title: `${companyName} Established`,
      body: "Incorporated and approved by Kemenkumham.",
      refText: co.sk_menhumkam_no,
      date: co.sk_menhumkam_date,
    });
  }

  const editAction = co?.company_id ? (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="h-8 w-8 text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
      aria-label="Edit company"
      title="Edit company"
      onClick={() => setIsEditingCompany(true)}
    >
      <Edit2 className="h-4 w-4" />
    </Button>
  ) : null;

  const syncDriveAction = co?.company_id ? (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="gap-1.5"
      disabled={isSyncingDrive}
      onClick={async () => {
        setIsSyncingDrive(true);
        try {
          const res = (await api.post(
            `/api/crm/companies/${co.company_id}/sync-drive`,
            {},
          )) as { added: number; skipped: number; total_in_folder: number };
          toast.success(
            `Drive sync: ${res.added} added, ${res.skipped} skipped`,
            {
              description: `${res.total_in_folder} files in folder`,
            },
          );
          if (res.added > 0) {
            reload();
            void onRefresh();
          }
        } catch (err) {
          toast.error("Drive sync failed", {
            description: (err as Error).message,
          });
        } finally {
          setIsSyncingDrive(false);
        }
      }}
    >
      {isSyncingDrive ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : (
        <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
      )}
      Sync Drive
    </Button>
  ) : null;

  const addCompanyAction = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="gap-1.5"
      onClick={() => setIsAddingCompany(true)}
    >
      <Plus className="h-3.5 w-3.5" aria-hidden="true" />
      Add company
    </Button>
  );

  // Section numbers follow what is actually rendered, so a hidden section
  // never leaves a gap (01, 03, 04...).
  const shownSections = [
    true,
    hasCapitalFacts,
    shareholderCount > 0,
    kbliCodes.length > 0,
    hasLegalData,
    Boolean(co?.company_id),
  ];
  const sectionN = (index: number) =>
    shownSections.slice(0, index + 1).filter(Boolean).length;

  // ── RENDER ─────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-8">
      <LedgerSection
        n={sectionN(0)}
        wrapActions
        tone={co ? "done" : "wait"}
        title="Company identity"
        actions={
          <>
            {editAction}
            {syncDriveAction}
            {addCompanyAction}
          </>
        }
      >
        <CompanyIdentity
          name={companyName}
          typeLabel={companyTypeLabel}
          typeGloss={companyTypeSubtitle}
          status={
            co ? companyStatusPill(co.company_status || "active") : undefined
          }
          foundingLines={[
            // The AHU decree number(s); a record holding two (deed, then the
            // latest amendment) separates them with " / ".
            ...(co?.sk_menhumkam_no || "")
              .split("/")
              .map((n) => n.trim())
              .filter(Boolean),
            ...(age ? [`${age.label} old`] : []),
          ]}
          nib={co?.nib}
          npwp={co?.npwp_company}
          capital={capitalFull}
          foundingDate={foundingDate ? formatDate(foundingDate) : undefined}
          address={streetAddress}
          cityProvince={cityProvince}
          documentCount={allDocs.length}
          aside={
            <AiRecap
              clientId={clientId}
              scope="company"
              companyId={co?.company_id}
              className="w-full sm:w-auto sm:min-w-[240px] sm:max-w-[52ch] sm:flex-1"
            />
          }
          onOpenDocuments={onOpenDocuments}
        />
        {otherLinks.length > 0 ? (
          <div className="pb-4">
            <p className="text-[11px] font-[650] uppercase tracking-[0.14em] text-[var(--tx-secondary)]">
              Also linked ({otherLinks.length})
            </p>
            <ul
              aria-label="Other linked companies"
              className="mt-2 flex flex-col gap-1 text-sm text-[var(--tx-secondary)]"
            >
              {otherLinks.map((l) => (
                <li key={l.link_id}>
                  <span className="text-[var(--tx-pure)]">
                    {l.company_name}
                  </span>
                  {l.role ? ` · ${l.role}` : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </LedgerSection>

      {hasCapitalFacts && (
        <LedgerSection n={sectionN(1)} title="Capital & shares">
          <div className={KV_GRID}>
            {capitalFull && (
              <KvItem
                label="Authorized capital"
                value={capitalFull}
                sub={
                  co?.akta_perubahan_no
                    ? `IDR · Increased via Akta #${co.akta_perubahan_no}${co.akta_perubahan_date ? ` · ${formatDate(co.akta_perubahan_date)}` : ""}`
                    : "IDR"
                }
              />
            )}
            {co?.shares_count ? (
              <KvItem
                label="Share structure"
                value={`${co.shares_count.toLocaleString()} shares`}
                sub={
                  co.share_nominal_value
                    ? `Rp ${(co.share_nominal_value / 1e6).toFixed(0)},000,000 par value per share`
                    : undefined
                }
              />
            ) : null}
            {co?.akta_perubahan_no ? (
              <KvItem
                label="Last amendment"
                value={`Akta #${co.akta_perubahan_no}`}
                sub={
                  co.akta_perubahan_date
                    ? `${formatDate(co.akta_perubahan_date)} · Capital restructuring`
                    : undefined
                }
              />
            ) : null}
          </div>
        </LedgerSection>
      )}

      {shareholderCount > 0 && (
        <LedgerSection
          n={sectionN(2)}
          title={
            <span className="flex items-baseline gap-2">
              Shareholders &amp; officers{" "}
              <Numeral n={shareholderCount} size="count" />
            </span>
          }
        >
          <HairlineGrid cols="minmax(12rem,1.8fr) minmax(8rem,1fr) minmax(6rem,0.7fr) minmax(6rem,0.7fr)">
            <HairlineHead className="max-sm:hidden">
              <span>Person</span>
              <span>Role</span>
              <span>Shares</span>
              <span>Ownership</span>
            </HairlineHead>
            <HairlineBody>
              {people.map((person, i) => {
                const name =
                  (person.name || client.full_name || "?").trim() || "?";
                const cardId = personClientId(person, associates, clientId);
                return (
                  <HairlineRow
                    key={`${name}-${i}`}
                    className="max-sm:grid-cols-3 max-sm:gap-y-1 max-sm:py-2.5"
                  >
                    <div className="min-w-0 px-2.5 max-sm:col-span-3">
                      <CellStack
                        primary={
                          cardId ? (
                            <Link
                              href={`/clients/${cardId}`}
                              className={cn(
                                "group/person flex min-w-0 items-center gap-3 rounded-[6px] text-[var(--tx-pure)]",
                                FOCUS,
                              )}
                            >
                              <span
                                aria-hidden="true"
                                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[6px] border border-[var(--bz-border)] bg-[var(--bz-card)] text-xs text-[var(--tx-pure)] group-hover/person:border-[var(--tx-pure)]"
                              >
                                {getInitials(name)}
                              </span>
                              <span
                                data-testid="person-name"
                                className={PERSON_NAME}
                                style={PERSON_NAME_STYLE}
                              >
                                {name}
                              </span>
                              <ArrowUpRight
                                className="h-3.5 w-3.5 shrink-0 text-[var(--tx-secondary)] group-hover/person:text-[var(--tx-pure)]"
                                aria-hidden="true"
                              />
                            </Link>
                          ) : (
                            <span className="flex min-w-0 items-center gap-3">
                              <span
                                aria-hidden="true"
                                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[6px] bg-[var(--bz-card)] text-xs text-[var(--tx-pure)]"
                              >
                                {getInitials(name)}
                              </span>
                              <span
                                data-testid="person-name"
                                className={PERSON_NAME}
                                style={PERSON_NAME_STYLE}
                              >
                                {name}
                              </span>
                            </span>
                          )
                        }
                      />
                    </div>
                    <span
                      className={cn(
                        "truncate px-2.5 capitalize text-[var(--tx-pure)]",
                        "max-sm:overflow-visible max-sm:whitespace-normal max-sm:break-words",
                        META_LABEL,
                        "max-sm:before:content-['Role_']",
                      )}
                    >
                      {person.role || "Shareholder"}
                    </span>
                    <span
                      className={cn(
                        "px-2.5",
                        META_LABEL,
                        "max-sm:before:content-['Shares_']",
                      )}
                      style={TABULAR}
                    >
                      {person.shares != null
                        ? person.shares.toLocaleString()
                        : "—"}
                    </span>
                    <span
                      className={cn(
                        "px-2.5",
                        META_LABEL,
                        "max-sm:before:content-['Ownership_']",
                      )}
                      style={TABULAR}
                    >
                      {person.pct != null ? `${person.pct}%` : "—"}
                    </span>
                  </HairlineRow>
                );
              })}
            </HairlineBody>
          </HairlineGrid>
        </LedgerSection>
      )}

      {kbliCodes.length > 0 && (
        <LedgerSection
          n={sectionN(3)}
          title={
            <span className="flex items-center gap-2.5">
              Business activities
              <a
                href="/kbli"
                target="_blank"
                rel="noopener"
                title="KBLI Navigator"
                className={cn("inline-flex rounded-full", FOCUS)}
              >
                <Image
                  src="/images/kita/kbli-navigator-badge.webp"
                  alt="KBLI Navigator"
                  width={30}
                  height={30}
                  unoptimized
                  className="h-[30px] w-[30px] rounded-full"
                />
              </a>
            </span>
          }
        >
          <HairlineGrid cols="96px minmax(0,1fr) 110px">
            <HairlineHead className="max-sm:grid-cols-[72px_minmax(0,1fr)] max-sm:[&>span:nth-child(3)]:hidden">
              <span>Code</span>
              <span>Activity</span>
              <span>Tag</span>
            </HairlineHead>
            <HairlineBody>
              {kbliCodes.map((code, i) => {
                const desc = kbliDescriptions[i] || kbliDescriptions[0] || "";
                const res = kbliResolved?.[code];
                const inNavigator = res?.inNavigator === true;
                const stale = res !== undefined && !inNavigator;
                const english = inNavigator
                  ? res.title
                  : stale
                    ? legacyEnglish[code]
                    : undefined;
                const successors = res?.successors ?? [];
                return (
                  <HairlineRow
                    key={code}
                    className="max-sm:grid-cols-[72px_minmax(0,1fr)] max-sm:py-2"
                  >
                    <span className="px-2.5 font-semibold" style={TABULAR}>
                      {inNavigator ? (
                        <a
                          href={`/kbli/${encodeURIComponent(code)}`}
                          target="_blank"
                          rel="noopener"
                          className={cn(
                            "inline-flex items-center gap-0.5 underline-offset-4 hover:underline",
                            FOCUS,
                          )}
                        >
                          {code}
                          <ArrowUpRight
                            className="h-3 w-3"
                            aria-hidden="true"
                          />
                          <span className="sr-only"> (opens in a new tab)</span>
                        </a>
                      ) : (
                        code
                      )}
                    </span>
                    <div className="min-w-0 px-2.5">
                      <CellStack
                        primary={english || desc || `KBLI ${code}`}
                        secondary={english ? desc : undefined}
                      />
                      {stale && (
                        <p
                          data-testid="kbli-2020-flag"
                          className="mt-0.5 text-[12px] text-[var(--state-warning)]"
                        >
                          KBLI 2020 · not yet migrated to KBLI 2025
                          {successors.length > 0 && (
                            <span className="block text-[var(--tx-secondary)]">
                              {successors.map((s) => (
                                <span key={s.code} className="block">
                                  → 2025:{" "}
                                  <a
                                    href={`/kbli/${s.code}`}
                                    target="_blank"
                                    rel="noopener"
                                    className={cn(
                                      "underline-offset-4 hover:underline",
                                      FOCUS,
                                    )}
                                  >
                                    {s.code}
                                  </a>{" "}
                                  {s.title}
                                </span>
                              ))}
                            </span>
                          )}
                        </p>
                      )}
                    </div>
                    <span className="px-2.5 max-sm:col-start-2 max-sm:pt-1">
                      <StatePill
                        tone={i === 0 ? "ink" : "wait"}
                        label={i === 0 ? "Primary" : "Secondary"}
                      />
                    </span>
                  </HairlineRow>
                );
              })}
            </HairlineBody>
          </HairlineGrid>
        </LedgerSection>
      )}

      {hasLegalData && (
        <LedgerSection n={sectionN(4)} title="Legal timeline">
          <LegalTimeline entries={legalEntries} formatDate={formatDate} />
        </LedgerSection>
      )}

      {co?.company_id && (
        <LedgerSection
          n={sectionN(5)}
          title={
            <span className="flex items-baseline gap-2">
              Document vault <Numeral n={allDocs.length} size="count" />
            </span>
          }
        >
          <div className="grid grid-cols-1 gap-3 py-5 sm:grid-cols-2 lg:grid-cols-3">
            {DOC_VAULT_SLOTS.map((item) => {
              // Search company docs first, then client docs with pma category
              const typeNorm = item.docType.toLowerCase().replace(/_/g, "");
              const matchType = (dt: string) => {
                const norm = (dt || "").toLowerCase().replace(/_/g, "");
                return norm === typeNorm || dt === item.docType;
              };
              const fromCompany = companyDocs.find((d) =>
                matchType(d.document_type),
              );
              const fromClient = !fromCompany
                ? (documents || [])
                    .filter((d) => d.document_category === "pma" && isLive(d))
                    .find((d) => matchType(d.document_type))
                : null;
              // Map ClientDocument to CompanyDocument-compatible shape for the vault slot
              const existing: CompanyDocument | null =
                fromCompany ??
                (fromClient
                  ? {
                      id: fromClient.id,
                      uuid: "",
                      document_type: fromClient.document_type,
                      status:
                        (fromClient.status as CompanyDocument["status"]) ??
                        "active",
                      google_drive_file_id: fromClient.file_id,
                      google_drive_file_url:
                        fromClient.google_drive_file_url ?? fromClient.file_url,
                      file_name: fromClient.file_name,
                      is_verified: fromClient.status === "verified",
                      created_at: fromClient.created_at ?? "",
                    }
                  : null);
              return (
                <CompanyDocUpload
                  key={item.docType}
                  clientId={clientId}
                  companyId={co.company_id!}
                  companyName={companyName}
                  docType={item.docType}
                  label={item.label}
                  hint={item.hint}
                  existingDoc={existing}
                  onUploaded={() => {
                    reload();
                    void onRefresh();
                  }}
                />
              );
            })}
          </div>
          <RemovedSignpost
            count={voidPmaDocs}
            onOpen={onOpenRemoved}
            className="pb-3"
          />
        </LedgerSection>
      )}

      {/* ── EDIT MODAL ─────────────────────────────────────────────────── */}
      {isEditingCompany && co?.company_id && (
        <EditCompanyModal
          companyId={co.company_id}
          initialData={co}
          onClose={() => setIsEditingCompany(false)}
          onSave={() => {
            setIsEditingCompany(false);
            reload();
          }}
        />
      )}

      {/* ── ADD COMPANY MODAL ──────────────────────────────────────────── */}
      {isAddingCompany && (
        <AddCompanyModal
          clientId={clientId}
          onClose={() => setIsAddingCompany(false)}
          onSuccess={() => {
            setIsAddingCompany(false);
            reload();
            void onRefresh();
          }}
        />
      )}
    </div>
  );
}

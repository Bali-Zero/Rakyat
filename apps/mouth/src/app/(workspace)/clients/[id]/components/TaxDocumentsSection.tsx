"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import type { ClientCompanyLink } from "@/lib/api/crm/crm.types";
import { MICRO_LABEL } from "@/components/workspace/r19";
import {
  FilesBrowserPanel,
  type FilesBrowserActions,
} from "./FilesBrowserPanel";

// "Tax Documents" on the Tax tab: the tax folders of the client and of each
// linked company, read live from Drive and shown with Drive's own folder
// names. Browsing and opening reuse FilesBrowserPanel, so files open through
// the kita proxy (/api/documents/proxy/{id}), never a drive.google.com URL.
//
// Folder names come from services/crm/drive_taxonomy.py (backend): an
// individual keeps tax papers in 03_Company_and_Tax (legacy alias 03_Tax), a
// company in 02_Tax_Returns_and_Payments (legacy alias
// 03_DICHIARAZIONI_E_RICEVUTE_FISCALI, COMPANY_ALIASES in drive_taxonomy.py);
// a company's tax_dept_folder_id (kind "restricted_tax" in /drive-sources)
// wins over all of them.

export const PERSONAL_TAX_FOLDERS = ["03_Company_and_Tax", "03_Tax"] as const;
export const COMPANY_TAX_FOLDER = "02_Tax_Returns_and_Payments";
/** Searched in this order, before falling back to the company root. */
export const COMPANY_TAX_FOLDERS = [
  COMPANY_TAX_FOLDER,
  "03_DICHIARAZIONI_E_RICEVUTE_FISCALI",
] as const;

type Resolved =
  | { status: "loading" }
  | { status: "none" }
  | { status: "error" }
  | { status: "ready"; folderId: string; rootName: string; note?: string };

/** `https://drive.google.com/drive/folders/<id>` → `<id>` (drive-sources URL). */
export function folderIdFromUrl(url: string): string | null {
  const m = /\/folders\/([A-Za-z0-9_-]+)/.exec(url);
  return m ? m[1] : null;
}

/** First direct subfolder of `parentId` whose name is in `names`, by `names` order. */
async function findSubfolder(
  parentId: string,
  names: readonly string[],
): Promise<{ id: string; name: string } | null> {
  const res = await api.drive.listFiles({
    folder_id: parentId,
    page_size: 100,
  });
  for (const name of names) {
    const hit = res.files.find((f) => f.is_folder && f.name === name);
    if (hit) return { id: hit.id, name: hit.name };
  }
  return null;
}

async function resolvePersonal(rootId: string): Promise<Resolved> {
  const sub = await findSubfolder(rootId, PERSONAL_TAX_FOLDERS);
  if (sub) return { status: "ready", folderId: sub.id, rootName: sub.name };
  return {
    status: "ready",
    folderId: rootId,
    rootName: "Client folder",
    note: "Tax folder not found, showing client folder",
  };
}

async function resolveCompany(companyId: number): Promise<Resolved> {
  const { sources } = await api.crm.getCompanyDriveSources(companyId);
  const idOf = (kind: "primary" | "restricted_tax") => {
    const s = sources.find((x) => x.kinds.includes(kind));
    return s ? folderIdFromUrl(s.url) : null;
  };
  const taxId = idOf("restricted_tax");
  if (taxId)
    return { status: "ready", folderId: taxId, rootName: "Tax folder" };
  const rootId = idOf("primary");
  if (!rootId) return { status: "none" };
  const sub = await findSubfolder(rootId, COMPANY_TAX_FOLDERS);
  if (sub) return { status: "ready", folderId: sub.id, rootName: sub.name };
  return {
    status: "ready",
    folderId: rootId,
    rootName: "Company folder",
    note: "Tax folder not found, showing company folder",
  };
}

function useResolved(
  key: string | null,
  resolve: () => Promise<Resolved>,
): Resolved {
  const [state, setState] = useState<Resolved>({ status: "loading" });
  useEffect(() => {
    if (key === null) {
      setState({ status: "none" });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    // Promise.resolve().then: a synchronous throw becomes an error state too.
    Promise.resolve()
      .then(resolve)
      .then(
        (r) => !cancelled && setState(r),
        () => !cancelled && setState({ status: "error" }),
      );
    return () => {
      cancelled = true;
    };
    // `resolve` is derived from `key`; re-run only when the key changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

function TaxFolderBlock({
  label,
  subject,
  state,
  actions,
}: {
  label: string;
  subject: "client" | "company";
  state: Resolved;
  actions?: FilesBrowserActions;
}) {
  return (
    <div data-testid="tax-folder-block">
      <p className={MICRO_LABEL}>{label}</p>
      {state.status === "loading" ? (
        <p className="flex items-center gap-2 py-3 text-[13px] text-[var(--tx-secondary)]">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Looking for the tax folder…
        </p>
      ) : state.status === "none" ? (
        <p className="py-3 text-[13px] text-[var(--tx-secondary)]">
          No Drive folder linked for this {subject}
        </p>
      ) : state.status === "error" ? (
        <p
          role="alert"
          className="py-3 text-[13px] text-[var(--state-warning)]"
        >
          Could not load the Drive folder.
        </p>
      ) : (
        <>
          {state.note ? (
            <p className="pt-2 text-[12px] text-[var(--tx-secondary)]">
              {state.note}
            </p>
          ) : null}
          <FilesBrowserPanel
            folderId={state.folderId}
            title={state.rootName}
            rootName={state.rootName}
            actions={actions}
          />
        </>
      )}
    </div>
  );
}

function PersonalBlock({
  folderId,
  actions,
}: {
  folderId?: string | null;
  actions?: FilesBrowserActions;
}) {
  const state = useResolved(folderId || null, () =>
    resolvePersonal(folderId as string),
  );
  return (
    <TaxFolderBlock
      label="Personal"
      subject="client"
      state={state}
      actions={actions}
    />
  );
}

function CompanyBlock({
  link,
  actions,
}: {
  link: ClientCompanyLink;
  actions?: FilesBrowserActions;
}) {
  const state = useResolved(String(link.company_id), () =>
    resolveCompany(link.company_id),
  );
  return (
    <TaxFolderBlock
      label={link.company_name}
      subject="company"
      state={state}
      actions={actions}
    />
  );
}

export function TaxDocumentsSection({
  clientFolderId,
  companyLinks,
  clientId,
  canManage = false,
  onMutated,
}: {
  clientFolderId?: string | null;
  companyLinks?: readonly ClientCompanyLink[];
  /** Client ID owning the Drive folders; required together with `canManage` to enable writes. */
  clientId?: number;
  /** True when the viewer is an admin or a team member associated with this client profile. */
  canManage?: boolean;
  /** Optional parent profile refresher invoked after rename, replace, or delete succeeds. */
  onMutated?: () => Promise<void> | void;
}) {
  // One block per company, even if the profile lists it under several roles.
  const companies = (companyLinks ?? []).filter(
    (l, i, all) => all.findIndex((x) => x.company_id === l.company_id) === i,
  );
  const actions = useMemo<FilesBrowserActions | undefined>(() => {
    if (!canManage || !clientId) return undefined;
    return {
      onUpload: (folderId, file) =>
        api.crm.uploadClientTaxDocument(clientId, folderId, file),
      onRename: async (file, newName) => {
        await api.crm.renameClientTaxDocument(clientId, file.id, newName);
        void onMutated?.();
      },
      onReplace: async (file, replacement, newName) => {
        await api.crm.replaceClientTaxDocument(
          clientId,
          file.id,
          replacement,
          newName,
        );
        void onMutated?.();
      },
      onDelete: async (file) => {
        await api.crm.deleteClientTaxDocument(clientId, file.id);
        void onMutated?.();
      },
    };
  }, [canManage, clientId, onMutated]);

  return (
    <div className="space-y-6 py-4">
      <PersonalBlock folderId={clientFolderId} actions={actions} />
      {companies.map((l) => (
        <CompanyBlock key={l.company_id} link={l} actions={actions} />
      ))}
    </div>
  );
}

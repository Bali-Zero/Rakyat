"use client";

// The one list of tab keys. TabType and the `?tab=` reader both derive from it,
// so a tab the page can write into the URL is always a tab it can read back.
//
// The ORDER here is the order of the tab bar: overview, practices (`process`),
// immigration (`visas`), company, tax, family, documents. There is no Activity
// tab (`timeline` / `whatsapp`) any more; a stale `?tab=timeline` link is not a
// TabType, so the reader ignores it and the page stays on Overview.
export const TAB_KEYS = [
  "overview",
  "process",
  "visas",
  "company",
  "tax",
  "family",
  "documents",
] as const;

export type TabType = (typeof TAB_KEYS)[number];

export function isTabType(value: string | null | undefined): value is TabType {
  return (TAB_KEYS as readonly string[]).includes(value ?? "");
}

export type ModalType =
  | "none"
  | "edit_client"
  | "add_family"
  | "edit_family"
  | "add_document"
  | "edit_document";

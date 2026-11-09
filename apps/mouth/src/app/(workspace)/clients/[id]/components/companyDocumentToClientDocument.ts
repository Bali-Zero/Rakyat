import type { ClientDocument, CompanyDocument } from "@/lib/api/crm/crm.types";

// Maps a linked company's document onto the ClientDocument shape the Tax tab
// reads, so corporate SPTs, Coretax credentials and EFINs light it up.
export function companyDocumentToClientDocument(
  cd: CompanyDocument,
  clientId: number,
): ClientDocument {
  const rawType = (cd.document_type || "").toLowerCase();
  let mappedType = cd.document_type;
  if (
    rawType === "spt_tahunan_badan" ||
    rawType === "spt_company" ||
    rawType === "bpe_spt_badan" ||
    rawType === "spt"
  ) {
    mappedType = "spt_company";
  } else if (
    rawType === "coretax_credentials" ||
    rawType === "coretax_access"
  ) {
    mappedType = "coretax_access";
  } else if (rawType === "efin_company" || rawType === "efin") {
    mappedType = "efin";
  }
  return {
    id: -cd.id,
    client_id: clientId,
    document_type: mappedType,
    document_category: "tax",
    file_name: cd.file_name || cd.document_title || `${mappedType}.pdf`,
    file_url: cd.google_drive_file_url,
    google_drive_file_url: cd.google_drive_file_url,
    file_id: cd.google_drive_file_id,
    status: cd.status || "active",
    expiry_date: cd.expiry_date,
    created_at: cd.created_at || "",
    company_id: cd.company_id,
    fiscal_year: cd.fiscal_year,
    is_archived: false,
    deleted_at: null,
  } as ClientDocument;
}

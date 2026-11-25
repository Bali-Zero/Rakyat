import { ApiError } from "../error-handler";

const MISSING_REQUIREMENTS =
  "No delivery requirements configured for this service";

export function isUnconfiguredDelivery(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    (error.detail === MISSING_REQUIREMENTS ||
      error.message === MISSING_REQUIREMENTS)
  );
}

export interface DeliverySlot {
  key: string;
  label: string;
  condition: string | null;
  document_id: number | null;
  file_name: string | null;
  waiver_reason: string | null;
  can_repair?: boolean;
  reusable_document_id: number | null;
  issued_on: string | null;
}

// Presentation names only. Stable keys and upload requirements stay server-owned.
export function deliveryDocumentTitle(
  slot: Pick<DeliverySlot, "key" | "label">,
): string {
  if (slot.key.startsWith("custom_")) return slot.label;
  if (slot.key === "electronic_visa") return "E-visa";
  const label = slot.label.split(" · ")[0].trim();
  const names: Record<string, string> = {
    "Filed SPT Tahunan": "SPT",
    "Bukti Penerimaan Elektronik (BPE)": "BPE — SPT submission receipt",
    "OSS filing acknowledgement": "LKPM submission proof",
    "OSS acknowledgement": "LKPM submission proof",
    "Issued SKCK": "SKCK",
    "Issued SKTT": "SKTT",
    "Issued RPTKA approval": "RPTKA approval",
    "Issued driving licence (SIM)": "SIM",
  };
  const permit = label.match(/^(?:Renewed )?(?:ITK|IMK|e-ITAS|e-ITAP)(?= — )/);
  return names[label] ?? permit?.[0] ?? label;
}

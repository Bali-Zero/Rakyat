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

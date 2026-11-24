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

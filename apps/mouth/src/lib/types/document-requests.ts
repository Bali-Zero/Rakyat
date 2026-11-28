/**
 * The reviewers' side of the document requests (V2, second cut): the shapes of
 * GET /api/crm/practices/document-requests/review and of the decision and reopen
 * routes, field for field with the backend contract.
 */

export type DocumentRequestDecisionState =
  "pending" | "added_to_card" | "case_only";

export type DocumentRequestCardState = "no_card" | "can_add" | "on_card";

export interface DocumentRequestReviewItem {
  id: number;
  practice_id: number;
  service_name: string | null;
  document_type: string;
  label: string;
  client_text: string;
  reason: string;
  requested_by: string;
  requested_at: string;
  seen_count: number;
  card_state: DocumentRequestCardState;
  card_live: boolean;
  decision: DocumentRequestDecisionState;
  decision_condition: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface DocumentRequestReviewList {
  items: DocumentRequestReviewItem[];
  total: number;
}

export interface DocumentRequestDecisionBody {
  decision: "added_to_card" | "case_only";
  condition?: string | null;
  client_text?: string | null;
}

export interface DocumentRequestDecisionResult {
  id: number;
  decision: string;
  decision_condition: string | null;
  decided_by: string;
  decided_at: string;
}

export interface DocumentRequestReopenResult {
  id: number;
  decision: "pending";
  card_document_removed: boolean;
}

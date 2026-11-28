// Required Documents Types for Process/Case Management

export type DocumentStatus = "pending" | "uploaded" | "verified" | "rejected";

export interface RequiredDocument {
  id: number;
  practice_id: number;
  document_type: string;
  document_label: string;
  description: string | null;
  is_required: boolean;
  uploaded_by_client: boolean;
  uploaded_file_id: number | null;
  uploaded_at: string | null;
  client_notes: string | null;
  team_member_notes: string | null;
  status: DocumentStatus;
  created_at: string;
  updated_at: string;
}

export interface RequiredDocumentCreate {
  document_type: string;
  document_label: string;
  description?: string;
  is_required?: boolean;
}

/** Ask the client for one more document, and say why this case needs it (V2). */
export interface DocumentRequestCreate {
  document_label: string;
  client_text: string;
  reason: string;
  /** Only when the typed name equals a suggestion; otherwise the server derives it. */
  document_type?: string;
}

export interface DocumentRequestResult {
  request_id: number;
  document: RequiredDocument;
}

/** Where a request stands for the card reviewers (the request's `decision`). */
export type DocumentRequestState = "pending" | "added_to_card" | "case_only";

/** One request made on a practice: its state, never its text, reason or client. */
export interface PracticeDocumentRequest {
  id: number;
  /** The slot the request created; null when the row has none. */
  required_document_id: number | null;
  state: DocumentRequestState;
  requested_by: string;
  requested_at: string;
}

/** The new text the client reads for a slot a request created. */
export interface DocumentRequestTextCorrection {
  client_text: string;
}

export interface RequiredDocumentUpdate {
  status?: DocumentStatus;
  team_member_notes?: string;
}

export interface ClientDocumentUpload {
  required_doc_id: number;
  file: string; // base64
  file_name: string;
  notes?: string;
}

export interface ClientRequiredDocument {
  id: number;
  practice_id: number;
  process_name: string;
  process_status: string;
  document_type: string;
  document_label: string;
  description: string | null;
  is_required: boolean;
  uploaded_by_client: boolean;
  status: DocumentStatus;
  client_notes: string | null;
  /** Present only on a slot a consultant's request created: who added it, and when. */
  case_request?: { added_by: string | null; added_at: string } | null;
}

// Predefined document types for selection
export const DOCUMENT_TYPE_OPTIONS = [
  { value: "passport", label: "Passport", description: "Valid passport copy" },
  { value: "photo", label: "Photo", description: "Recent passport-size photo" },
  {
    value: "bank_statement",
    label: "Bank Statement",
    description: "Last 3 months bank statement",
  },
  { value: "cv", label: "CV / Resume", description: "Curriculum Vitae" },
  {
    value: "diploma",
    label: "Diploma / Degree",
    description: "Educational certificates",
  },
  {
    value: "employment_letter",
    label: "Employment Letter",
    description: "Letter from current employer",
  },
  {
    value: "police_clearance",
    label: "Police Clearance",
    description: "Police clearance certificate",
  },
  {
    value: "medical_certificate",
    label: "Medical Certificate",
    description: "Health check certificate",
  },
  {
    value: "insurance",
    label: "Insurance",
    description: "Travel/health insurance",
  },
  {
    value: "flight_ticket",
    label: "Flight Ticket",
    description: "Confirmed flight booking",
  },
  {
    value: "hotel_booking",
    label: "Hotel Booking",
    description: "Accommodation confirmation",
  },
  {
    value: "invitation_letter",
    label: "Invitation Letter",
    description: "Invitation from sponsor/host",
  },
  {
    value: "business_license",
    label: "Business License",
    description: "Company business license",
  },
  {
    value: "deed_of_establishment",
    label: "Deed of Establishment",
    description: "AKTA Pendirian",
  },
  { value: "nib", label: "NIB", description: "Nomor Induk Berusaha" },
  { value: "npwp", label: "NPWP", description: "Tax identification number" },
  {
    value: "sk_kemenkumham",
    label: "SK Kemenkumham",
    description: "Ministry of Law approval",
  },
  { value: "other", label: "Other", description: "Custom document type" },
] as const;

export type DocumentTypeOption =
  (typeof DOCUMENT_TYPE_OPTIONS)[number]["value"];

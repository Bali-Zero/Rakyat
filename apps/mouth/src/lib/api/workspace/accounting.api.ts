/**
 * Workspace Accounting API — cash-control endpoints for Asya (the accountant).
 *
 * Mirrors the backend accounting router (apps/backend-rag/backend/app/routers/
 * accounting.py). RBAC is enforced server-side (is_crm_admin → 403 otherwise);
 * Asya@balizero.com is in CRM_EXTRA_ADMIN_EMAILS. Money is IDR (integer rupiah).
 *
 * Endpoints return FLAT shapes (list[dict] / dict), NOT wrapped — the types here
 * are derived from cashout_service.py + reconcile_service.py response columns.
 */

import { api } from "@/lib/api";

// ── Weekly cashout row (mirrors Asya's "Weekly Cashout" sheet) ───────────────
export interface CashoutRow {
  id: number;
  movement_date: string | null;
  week_label: string | null;
  direction: "in" | "out";
  type: string;
  amount_idr: number;
  pnbp_idr: number;
  urgent_idr: number;
  rptka_imta_idr: number;
  margin_idr: number;
  final_price_idr: number;
  original_currency: string | null;
  original_amount: number | null;
  fx_rate: number | null;
  fx_date: string | null;
  pph_withheld_idr: number;
  bank_fee_idr: number;
  linked_practice_id: number | null;
  linked_invoice_id: number | null;
  category: string | null;
  description: string | null;
  counterparty: string | null;
  payment_method: string | null;
  cash_company_name?: string | null;
  recorded_by: string | null;
  recorded_at: string | null;
  reversed_by_id: number | null;
  client_name: string | null;
  /** The service of the linked case (absent from an older backend). */
  service_name?: string | null;
}

export type ReconciledStatus = "unmatched" | "matched" | "manual" | "ignored";

export interface BankTransaction {
  id: number;
  statement_id: number | null;
  txn_date: string;
  description: string | null;
  amount_idr: number;
  direction: "credit" | "debit";
  running_balance_idr: number | null;
  bank_account_id: number | null;
  reconciled_status: ReconciledStatus;
  matched_practice_id: number | null;
  matched_invoice_id: number | null;
  created_at: string | null;
}

export interface CashbookSummary {
  income_idr: number;
  outgoing_idr: number;
  net_idr: number;
  margin_total_idr: number;
  pnbp_total_idr: number;
  row_count: number;
  by_type: Array<{
    type: string;
    direction: "in" | "out";
    total_idr: number;
    n: number;
  }>;
}

export interface MatchCandidate {
  invoice_id: number;
  practice_id: number;
  client_name: string;
  invoice_amount_idr: number;
  confidence: number; // 0.0 – 1.0
  reasons: string[];
  amount_delta_idr: number;
  likely_withheld: boolean;
  /** Identity of the invoice; null when the source has none, absent from an older backend. */
  invoice_number?: string | null;
  /** ISO 8601 stamp of the invoice's generated_at. */
  invoice_issued_at?: string | null;
  service_name?: string | null;
  /** Already received on the invoice; absent from an older backend (read as 0). */
  paid_idr?: number;
  /** What is still owed (invoice amount - paid_idr); absent from an older backend. */
  balance_idr?: number;
}

export interface MatchCandidatesResponse {
  txn_id: number;
  transfer_amount_idr: number;
  candidates: MatchCandidate[];
  /** Candidates among payments a practice already records (link, never confirm). */
  recorded_candidates: MatchCandidate[];
}

/** Advice only: the screen never acts on a light by itself. */
export type InboxLight = "green" | "recorded" | "yellow" | "red";

export type DoneTodayAction = "confirmed" | "linked" | "ignored";

/** One transfer handled today (GET /done-today). */
export interface DoneTodayItem {
  txn_id: number;
  action: DoneTodayAction;
  /** WITA local time, ISO with a +08:00 offset. */
  done_at: string;
  txn_date: string;
  amount_idr: number;
  description: string;
  invoice_number: string | null;
  client_name: string | null;
}

export interface DoneTodayResponse {
  /** The WITA day, YYYY-MM-DD. */
  day: string;
  timezone: string;
  counts: {
    confirmed: number;
    linked: number;
    ignored: number;
    total: number;
  };
  items: DoneTodayItem[];
  truncated: boolean;
}

/** One issuing company with its bank account and imported-statement coverage (GET /companies). */
export interface AccountingCompany {
  /** An issuing-entity key, or "other" for imported accounts no company owns. */
  key: string;
  company_name: string;
  bank_name: string | null;
  account_number: string | null;
  /** Last day covered by an imported statement (YYYY-MM-DD), null when none is imported. */
  data_through: string | null;
  statements: number;
  /** Incoming transfers of this account still waiting to be reconciled. */
  to_reconcile: number;
}

export interface CashInvoice {
  invoice_id: number;
  invoice_number: string;
  practice_id: number;
  client_name: string;
  service_name: string;
  amount_idr: number;
  outstanding_idr: number;
}

export interface CashReceiptResponse {
  cashout_id: number;
  status_after: string;
  outstanding_idr: number;
}

/** One unmatched incoming transfer with its light (GET /reconciliation-inbox). */
export interface ReconciliationInboxItem {
  txn_id: number;
  txn_date: string;
  amount_idr: number;
  /** Already applied by confirmations; what is left to match is amount_idr - applied_idr (absent from an older backend, read as 0). */
  applied_idr?: number;
  description: string;
  /** The payer the bank line names, "" when it names none (absent from an older backend). */
  payer_name?: string;
  /** The issuing company whose bank account received it; null when no company owns the account. */
  company_key: string | null;
  light: InboxLight;
  open_candidates: number;
  open_strong: number;
  recorded_candidates: number;
  recorded_strong: number;
  suggestion: MatchCandidate | null;
}

export interface LinkRecordedRequest {
  bank_txn_id: number;
  practice_id: number;
  invoice_id: number;
}

export interface IgnoreTransactionRequest {
  bank_txn_id: number;
  /** 3 to 200 characters after trimming. */
  reason: string;
}

export interface ReconciliationActionResponse {
  bank_txn_id: number;
  reconciled_status: string;
  reconciliation_log_id: number | null;
}

export interface ConfirmRequest {
  bank_txn_id?: number | null;
  practice_id: number;
  invoice_id?: number | null;
  amount_applied_idr: number;
  new_status?: "unpaid" | "partial" | "paid";
  payment_method?: string;
  payment_reference?: string | null;
  pph_withheld_idr?: number;
  bank_fee_idr?: number;
  pnbp_idr?: number;
  urgent_idr?: number;
  rptka_imta_idr?: number;
  margin_idr?: number;
  movement_date?: string | null;
  /** Historical mode: recorded without emails to the client or the team. */
  historical?: boolean;
}

export interface ConfirmResponse {
  practice_id: number;
  invoice_id: number | null;
  cashout_id: number;
  reconciliation_log_id: number;
  status_before: string | null;
  status_after: string;
  historical?: boolean;
}

export interface ExportSheetResponse {
  spreadsheet_id: string;
  rows_exported: number;
  updated_cells: number;
  range: string;
}

export interface ImportCashoutResponse {
  source_filename: string;
  imported: number;
  weeks: string[];
  replaced: number;
}

/** Amounts in a statement report are 2-decimal strings (e.g. "1500000.00"). */
/** What the bank's text says about one debit (GET /outgoings); never stored. */
export interface OutgoingReading {
  label: string;
  detail: string;
  payee?: string | null;
  purpose?: string;
  billing_code?: string;
  /** PNBP only: debits carrying the same e-billing code; 2 or more is a possible double payment. */
  code_debits?: number;
  cheque?: string;
}

export interface OutgoingItem {
  id: number;
  txn_date: string;
  posted_at: string | null;
  amount_idr: number;
  kind: string;
  description: string;
  company_key: string;
  reading: OutgoingReading;
  /** A transfer fee: the transfer it was charged for. */
  fee_of_txn_id: number | null;
  state: OutgoingState;
  /** The active justification; for a fee that follows its transfer, that transfer's. */
  justification: OutgoingJustification | null;
}

export type OutgoingState = "to_justify" | "justified";

export type OutgoingCategory =
  | "pnbp_case"
  | "vendor_case"
  | "staff_reimbursement"
  | "payroll"
  | "cash_withdrawal"
  | "bank_charges"
  | "tax"
  | "own_transfer"
  | "other";

/** One line of a cash withdrawal's breakdown; the lines sum to the debit. */
/** The case a justification points to, as the accountant reads it. */
export interface OutgoingCase {
  client: string | null;
  service: string | null;
}

export interface OutgoingJustifyLine {
  amount_idr: number;
  category: OutgoingCategory;
  note: string;
  practice_id?: number | null;
  /** Read only: sent by the list, never in a request. */
  case?: OutgoingCase | null;
}

export interface OutgoingJustification {
  category: OutgoingCategory;
  practice_id: number | null;
  /** Client name and service of practice_id, when the case still exists. */
  case?: OutgoingCase | null;
  recipient: string | null;
  note: string | null;
  lines: OutgoingJustifyLine[];
  by: string;
  at: string;
  /** Set on a fee: the transfer whose justification covers it. */
  follows_txn_id?: number;
  /** Set when justified with «Justify all PNBP»: the rows of one click share it. */
  batch_id?: string | null;
  batch_active_count?: number;
}

/** POST /outgoings/pnbp-batch: the PNBP debits justified in one go. */
export interface PnbpBatchResponse {
  batch_id: string;
  n: number;
  total_idr: number;
  state: OutgoingState;
}

/** POST /outgoings/batches/{id}/undo: how many rows of the batch were taken back. */
export interface OutgoingBatchUndoResponse {
  batch_id: string;
  n: number;
  state: OutgoingState;
}

/** Body of POST /outgoings/{id}/justify; absent optional keys are left out. */
export interface JustifyOutgoingRequest {
  category: OutgoingCategory;
  practice_id?: number;
  recipient?: string;
  note?: string;
  lines?: OutgoingJustifyLine[];
}

export interface OutgoingActionResponse {
  txn_id: number;
  justification_id: number;
  state: OutgoingState;
}

export interface OutgoingKindTotal {
  kind: string;
  label: string;
  n: number;
  total_idr: number;
}

export interface OutgoingsResponse {
  items: OutgoingItem[];
  by_kind: OutgoingKindTotal[];
  /** Still to justify. */
  n: number;
  total_idr: number;
  n_justified: number;
  justified_total_idr: number;
}

export interface BankStatementKindSum {
  count: number;
  total: string;
}

/** Report of a parsed statement (bank_parser.build_preview_report). */
export interface BankStatementPreview {
  account: { number: string; name: string };
  period: { first: string; last: string };
  row_count: number;
  opening_balance: string;
  closing_balance: string;
  total_debit: string;
  total_credit: string;
  balance_check: { ok: boolean; mismatch_positions: number[] };
  order_decided_by: string;
  kinds: Record<string, BankStatementKindSum>;
}

export interface BankStatementImportOutcome {
  statement_id: number | null;
  rows_inserted: number;
  rows_duplicate: number;
  history_segments: number;
  nothing_new: boolean;
}

export interface BankStatementImportResponse extends BankStatementPreview {
  import: BankStatementImportOutcome;
}

/**
 * Multipart POST of one `file` field. fetch sets the multipart boundary; we only
 * attach CSRF + Authorization and send httpOnly cookies. A non-OK response
 * throws an Error whose message is the backend's `detail` (or the statusText).
 */
async function postFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);

  const headers: Record<string, string> = {};
  const csrf = api.getCsrfToken();
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const token = api.getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const resp = await fetch(`${api.getBaseUrl()}${path}`, {
    method: "POST",
    headers,
    body: form,
    credentials: "include",
  });

  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return resp.json() as Promise<T>;
}

const qs = (params: Record<string, string | number | undefined>): string => {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
};

export const accountingApi = {
  /** Weekly cashout rows (Asya's sheet), newest first. */
  async getCashout(opts?: {
    month?: string;
    weekLabel?: string;
    type?: string;
    limit?: number;
  }): Promise<CashoutRow[]> {
    return api.get<CashoutRow[]>(
      `/api/crm/accounting/cashout${qs({
        month: opts?.month,
        week_label: opts?.weekLabel,
        type: opts?.type,
        limit: opts?.limit,
      })}`,
    );
  },

  /** Parsed bank-statement transactions, newest first; the reconciliation inbox. */
  async getBankTransactions(opts?: {
    month?: string;
    reconciledStatus?: ReconciledStatus;
    limit?: number;
  }): Promise<BankTransaction[]> {
    return api.get<BankTransaction[]>(
      `/api/crm/accounting/bank-transactions${qs({
        month: opts?.month,
        reconciled_status: opts?.reconciledStatus,
        limit: opts?.limit,
      })}`,
    );
  },

  /** What was confirmed, linked or ignored today (WITA); read-only. */
  async getDoneToday(month?: string): Promise<DoneTodayResponse> {
    return api.get<DoneTodayResponse>(
      `/api/crm/accounting/done-today${qs({ month })}`,
    );
  },

  /** The issuing companies, each with its bank account and how far its statements reach. */
  async getCompanies(month?: string): Promise<AccountingCompany[]> {
    return api.get<AccountingCompany[]>(
      `/api/crm/accounting/companies${qs({ month })}`,
    );
  },

  async getCashInvoices(companyKey: string, q: string): Promise<{ items: CashInvoice[] }> {
    return api.get(`/api/crm/accounting/cash-invoices${qs({ company_key: companyKey, q })}`);
  },

  async createCashReceipt(payload: {
    company_key: string; invoice_id: number; amount_idr: number; movement_date: string;
    reference?: string; request_id: string;
  }): Promise<CashReceiptResponse> {
    return api.post("/api/crm/accounting/cash-receipts", payload);
  },

  /** Debits newest first with their reading and state; `state` lists one side only. */
  async getOutgoings(opts?: {
    companyKey?: string;
    month?: string;
    limit?: number;
    state?: OutgoingState;
  }): Promise<OutgoingsResponse> {
    return api.get<OutgoingsResponse>(
      `/api/crm/accounting/outgoings${qs({ company_key: opts?.companyKey, limit: opts?.limit, state: opts?.state, month: opts?.month })}`,
    );
  },

  /** Justify one debit. Writes nothing to the cash-out. 404/409/422 carry a `detail` message. */
  async justifyOutgoing(
    txnId: number,
    body: JustifyOutgoingRequest,
  ): Promise<OutgoingActionResponse> {
    return api.post<OutgoingActionResponse>(
      `/api/crm/accounting/outgoings/${txnId}/justify`,
      body,
    );
  },

  /**
   * Justify the listed PNBP debits in one go (no case), all or nothing; at most
   * 500 ids. 404/409/422 carry a `detail` message.
   */
  async justifyPnbpBatch(txnIds: number[]): Promise<PnbpBatchResponse> {
    return api.post<PnbpBatchResponse>(
      `/api/crm/accounting/outgoings/pnbp-batch`,
      { txn_ids: txnIds },
    );
  },

  /** Take back what is still active of a batch made today (WITA). */
  async undoOutgoingBatch(batchId: string): Promise<OutgoingBatchUndoResponse> {
    return api.post<OutgoingBatchUndoResponse>(
      `/api/crm/accounting/outgoings/batches/${encodeURIComponent(batchId)}/undo`,
    );
  },

  /** Take back a justification made today (WITA). */
  async undoOutgoingJustification(
    txnId: number,
  ): Promise<OutgoingActionResponse> {
    return api.post<OutgoingActionResponse>(
      `/api/crm/accounting/outgoings/${txnId}/undo`,
    );
  },

  /** Unmatched incoming transfers with their light, newest first. */
  async getReconciliationInbox(
    limit?: number,
    month?: string,
  ): Promise<ReconciliationInboxItem[]> {
    return api.get<ReconciliationInboxItem[]>(
      `/api/crm/accounting/reconciliation-inbox${qs({ limit, month })}`,
    );
  },

  /** Attach a transfer to the invoice of a payment already recorded; it enters the cash-out. */
  async linkRecorded(
    payload: LinkRecordedRequest,
  ): Promise<ReconciliationActionResponse> {
    return api.post<ReconciliationActionResponse>(
      "/api/crm/accounting/link-recorded",
      payload,
    );
  },

  /** Mark a transfer as not a client payment. Moves no money. */
  async ignoreTransaction(
    payload: IgnoreTransactionRequest,
  ): Promise<ReconciliationActionResponse> {
    return api.post<ReconciliationActionResponse>(
      "/api/crm/accounting/ignore-transaction",
      payload,
    );
  },

  /** Put a linked or ignored transfer back to unmatched. */
  async reopenTransaction(
    bankTxnId: number,
  ): Promise<ReconciliationActionResponse> {
    return api.post<ReconciliationActionResponse>(
      "/api/crm/accounting/reopen-transaction",
      { bank_txn_id: bankTxnId },
    );
  },

  /** Take back a payment confirmed today (WITA): everything it recorded goes back. Sends no email. */
  async undoConfirmation(
    bankTxnId: number,
  ): Promise<ReconciliationActionResponse> {
    return api.post<ReconciliationActionResponse>(
      "/api/crm/accounting/undo-confirmation",
      { bank_txn_id: bankTxnId },
    );
  },

  /** Cash-basis P&L summary over the cashout log. */
  async getSummary(opts?: {
    periodStart?: string;
    periodEnd?: string;
  }): Promise<CashbookSummary> {
    return api.get<CashbookSummary>(
      `/api/crm/accounting/summary${qs({
        period_start: opts?.periodStart,
        period_end: opts?.periodEnd,
      })}`,
    );
  },

  /** Candidate invoices for one incoming (credit) transfer. Never auto-confirms. */
  async getMatchCandidates(txnId: number): Promise<MatchCandidatesResponse> {
    return api.get<MatchCandidatesResponse>(
      `/api/crm/accounting/match-candidates/${txnId}`,
    );
  },

  /** Confirm a payment match. Single writer of payment_status (superscar #9). */
  async confirm(payload: ConfirmRequest): Promise<ConfirmResponse> {
    return api.post<ConfirmResponse>("/api/crm/accounting/confirm", payload);
  },

  /**
   * On-demand push of the weekly cashout to Zero's Google Sheet (NOT auto-sync).
   * The sheet is pre-created + shared with the service account by Zero; the
   * backend writes into it. 503 if ACCOUNTING_EXPORT_SHEET_ID is not configured.
   */
  async exportSheet(weekLabel?: string): Promise<ExportSheetResponse> {
    return api.post<ExportSheetResponse>(
      `/api/crm/accounting/export-sheet${qs({ week_label: weekLabel })}`,
    );
  },

  /**
   * Import Asya's weekly cashout worksheet PDF (GABUNGAN) into the cashout log.
   * Multipart upload — fetch sets the multipart boundary; we only attach CSRF +
   * Authorization and send httpOnly cookies. Idempotent on the backend
   * (re-uploading a file replaces that week's prior import rows). Imported rows
   * land as type='cashout_worksheet' (a draft, excluded from the cash totals).
   * 422 if the PDF carries no "NEW CASHOUT" title / no client rows.
   */
  async importCashout(file: File): Promise<ImportCashoutResponse> {
    return postFile<ImportCashoutResponse>(
      "/api/crm/accounting/import-cashout",
      file,
    );
  },

  /**
   * Preview a bank statement (CIMB CSV, Mandiri or Permata PDF; max 2 MB, admin only). Stores nothing; returns
   * the report of the statement. 422 carries a fixed-text `detail`.
   */
  async previewBankStatement(file: File): Promise<BankStatementPreview> {
    return postFile<BankStatementPreview>(
      "/api/crm/accounting/bank-statement/preview",
      file,
    );
  },

  /**
   * Store a bank statement: the preview report plus an `import` result.
   * Idempotent: sending the same file again stores nothing new.
   */
  async importBankStatement(file: File): Promise<BankStatementImportResponse> {
    return postFile<BankStatementImportResponse>(
      "/api/crm/accounting/bank-statement/import",
      file,
    );
  },
};

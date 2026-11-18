import type {
  BankStatementImportResponse,
  MatchCandidate,
  ReconciliationInboxItem,
  BankStatementPreview,
} from "@/lib/api/workspace/accounting.api";

// Every value is invented.
export const previewFixture = (
  over: Partial<BankStatementPreview> = {},
): BankStatementPreview => ({
  account: { number: "0000000000", name: "CONTOH PT" },
  period: { first: "2026-01-02", last: "2026-01-30" },
  row_count: 3,
  opening_balance: "1000000.00",
  closing_balance: "1500000.00",
  total_debit: "200000.00",
  total_credit: "700000.00",
  balance_check: { ok: true, mismatch_positions: [] },
  order_decided_by: "balance_chain",
  kinds: {
    transfer_in: { count: 2, total: "700000.00" },
    fee: { count: 1, total: "200000.00" },
  },
  ...over,
});

export const importFixture = (
  outcome: Partial<BankStatementImportResponse["import"]> = {},
): BankStatementImportResponse => ({
  ...previewFixture(),
  import: {
    statement_id: 1,
    rows_inserted: 3,
    rows_duplicate: 0,
    history_segments: 1,
    nothing_new: false,
    ...outcome,
  },
});

export const inboxItem = (
  over: Partial<ReconciliationInboxItem> = {},
): ReconciliationInboxItem => ({
  txn_id: 1,
  txn_date: "2026-01-05",
  amount_idr: 500000,
  description: "TRANSFER CONTOH",
  light: "yellow",
  open_candidates: 0,
  open_strong: 0,
  recorded_candidates: 0,
  recorded_strong: 0,
  suggestion: null,
  ...over,
});

export const candidate = (
  over: Partial<MatchCandidate> = {},
): MatchCandidate => ({
  invoice_id: 11,
  practice_id: 21,
  client_name: "CONTOH PT",
  invoice_amount_idr: 500000,
  confidence: 0.9,
  reasons: ["amount matches"],
  amount_delta_idr: 0,
  likely_withheld: false,
  ...over,
});

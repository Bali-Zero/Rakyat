import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

const partners = vi.hoisted(() => ({
  getPartner: vi.fn(),
  listReferrals: vi.fn(),
  listCommissions: vi.fn(),
  listAuditLog: vi.fn(),
  activatePartner: vi.fn(),
  deactivatePartner: vi.fn(),
  reassignPartner: vi.fn(),
}));

vi.mock("@/lib/api/partners/partners", () => partners);
vi.mock("@/lib/api", () => ({ api: { isAdmin: () => false } }));
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "aaaaaaaa-0000-0000-0000-000000000001" }),
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => <a href={href}>{children}</a>,
}));

import PartnerDetailPage from "./page";

const PARTNER = {
  id: "aaaaaaaa-0000-0000-0000-000000000001",
  full_name: "Zz Partner",
  email: "partner@example.test",
  entity_type: "individual",
  onboarding_status: "active",
  tax_withholding_category: "pph23",
  default_commission_type: "percentage",
  default_commission_value: "10.0000",
  bank_name: "Bank Lama",
  created_at: "2026-02-01T08:00:00Z",
};

// The two rows below are the server's answer, character for character: the
// backend test tests/routers/test_partners_list_shapes.py pins the same rows.
const REFERRAL = {
  id: "bbbbbbbb-0000-0000-0000-000000000001",
  partner_id: "aaaaaaaa-0000-0000-0000-000000000001",
  practice_id: 42,
  share_percent: "100.00",
  referred_at: "2026-03-01T08:00:00Z",
  referred_by_user_id: "team-1",
  notes: "Met at the expo",
};
const COMMISSION = {
  id: "cccccccc-0000-0000-0000-000000000001",
  partner_id: "aaaaaaaa-0000-0000-0000-000000000001",
  entry_type: "accrual",
  base_amount_idr: "12500000.00",
  commission_type_snapshot: "percentage",
  commission_value_snapshot: "10.0000",
  rule_source: "partner_default",
  gross_amount_idr: "1250000.00",
  withholding_category: "pph23",
  withholding_rate: "0.0200",
  withholding_amount_idr: "25000.00",
  net_amount_idr: "1225000.00",
  status: "accrued",
  accrued_at: "2026-03-01T08:00:00Z",
  eligible_for_approval_at: "2026-03-01T08:00:00Z",
  created_at: "2026-03-01T08:00:00Z",
  referral_id: "bbbbbbbb-0000-0000-0000-000000000001",
  practice_id: 42,
  related_commission_id: null,
  assigned_to_snapshot: null,
  approved_at: null,
  approved_by: null,
  paid_at: null,
  paid_by: null,
  paid_via: null,
  payment_reference: null,
  payment_proof_url: null,
  receipt_type: null,
  receipt_file_url: null,
  manual_override_reason: null,
  clawback_reason: null,
  waiver_reason: null,
  idempotency_key: null,
  commission_email_sent_at: null,
};
const DAY = new Date("2026-03-01T08:00:00Z").toLocaleDateString();

async function openTab(name: string) {
  render(<PartnerDetailPage />);
  fireEvent.click(await screen.findByRole("button", { name }));
}

/** The cells of the table row that holds this text. */
function rowOf(text: string): string[] {
  const row = screen.getByText(text).closest("tr")!;
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent ?? "");
}

beforeEach(() => {
  vi.clearAllMocks();
  partners.getPartner.mockResolvedValue(PARTNER);
  partners.listReferrals.mockResolvedValue([]);
  partners.listCommissions.mockResolvedValue([]);
  partners.listAuditLog.mockResolvedValue([]);
});

describe("the Referrals tab of a partner", () => {
  it("lists the rows the server sends", async () => {
    partners.listReferrals.mockResolvedValue([REFERRAL]);

    await openTab("Referrals");

    await waitFor(() =>
      expect(rowOf("Practice #42")).toEqual([
        "Practice #42",
        "100%",
        DAY,
        "Met at the expo",
      ]),
    );
    expect(partners.listReferrals).toHaveBeenCalledWith(
      "aaaaaaaa-0000-0000-0000-000000000001",
    );
  });

  it("shows a share that is not whole, and a dash for a referral with no note", async () => {
    partners.listReferrals.mockResolvedValue([
      { ...REFERRAL, practice_id: 7, share_percent: "12.50", notes: null },
    ]);

    await openTab("Referrals");

    await waitFor(() =>
      expect(rowOf("Practice #7")).toEqual(["Practice #7", "12.5%", DAY, "—"]),
    );
  });

  it("says there are none when the server sends an empty list", async () => {
    await openTab("Referrals");

    expect(await screen.findByText("No referrals yet")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says the list did not load, and loads it on a retry", async () => {
    partners.listReferrals.mockRejectedValueOnce(new Error("503"));

    await openTab("Referrals");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Referrals could not be loaded.",
    );
    expect(screen.queryByText("No referrals yet")).toBeNull();

    partners.listReferrals.mockResolvedValue([REFERRAL]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Practice #42")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(partners.listReferrals).toHaveBeenCalledTimes(2);
  });

  it("treats an answer that is not a list as a list that did not load", async () => {
    partners.listReferrals.mockResolvedValue({
      referrals: [REFERRAL],
      total: 1,
    });

    await openTab("Referrals");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Referrals could not be loaded.",
    );
    expect(screen.queryByText("No referrals yet")).toBeNull();
  });
});

describe("the Commissions tab of a partner", () => {
  it("lists the rows the server sends, with their amounts", async () => {
    partners.listCommissions.mockResolvedValue([COMMISSION]);

    await openTab("Commissions");

    await waitFor(() => expect(rowOf("Practice #42")).toHaveLength(6));
    const [practice, entry, gross, net, status, date] = rowOf("Practice #42");
    expect(practice).toBe("Practice #42");
    expect(entry).toBe("accrual");
    expect(gross.replace(/\s/g, " ")).toBe("Rp 1.250.000");
    expect(net.replace(/\s/g, " ")).toBe("Rp 1.225.000");
    expect(status).toBe("accrued");
    expect(date).toBe(DAY);
  });

  it("shows an entry with no practice, and a clawback with its sign", async () => {
    partners.listCommissions.mockResolvedValue([
      {
        ...COMMISSION,
        id: "cccccccc-0000-0000-0000-000000000002",
        entry_type: "manual_adjustment",
        practice_id: null,
        gross_amount_idr: "500000.00",
        net_amount_idr: "490000.00",
        status: "approved",
      },
      {
        ...COMMISSION,
        id: "cccccccc-0000-0000-0000-000000000003",
        entry_type: "clawback",
        practice_id: 9,
        gross_amount_idr: "-250000.00",
        net_amount_idr: "-245000.00",
        status: "clawback_pending",
      },
    ]);

    await openTab("Commissions");

    await waitFor(() => expect(rowOf("manual adjustment")).toHaveLength(6));
    const adjustment = rowOf("manual adjustment");
    expect(adjustment[0]).toBe("—");
    expect(adjustment[2].replace(/\s/g, " ")).toBe("Rp 500.000");
    expect(adjustment[4]).toBe("approved");
    const clawback = rowOf("Practice #9");
    expect(clawback[1]).toBe("clawback");
    expect(clawback[2]).toMatch(/^-\s?Rp\s?250\.000$/);
    expect(clawback[3]).toMatch(/^-\s?Rp\s?245\.000$/);
    expect(clawback[4]).toBe("clawback pending");
  });

  it("says there are none when the server sends an empty list", async () => {
    await openTab("Commissions");

    expect(await screen.findByText("No commissions yet")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says the list did not load, and loads it on a retry", async () => {
    partners.listCommissions.mockRejectedValueOnce(new Error("503"));

    await openTab("Commissions");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Commissions could not be loaded.",
    );
    expect(screen.queryByText("No commissions yet")).toBeNull();

    partners.listCommissions.mockResolvedValue([COMMISSION]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Practice #42")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("treats an answer that is not a list as a list that did not load", async () => {
    partners.listCommissions.mockResolvedValue({
      commissions: [COMMISSION],
      total: 1,
    });

    await openTab("Commissions");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Commissions could not be loaded.",
    );
  });
});

describe("the Profile and Payment tabs of a partner", () => {
  it("show only what a partner record holds", async () => {
    render(<PartnerDetailPage />);

    expect(await screen.findByText("Full Name")).toBeInTheDocument();
    expect(screen.queryByText("WhatsApp")).toBeNull();
    expect(screen.queryByText("Nationality")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Payment" }));

    expect(await screen.findByText("Bank Name")).toBeInTheDocument();
    expect(screen.getByText("Bank Lama")).toBeInTheDocument();
    expect(screen.queryByText("Payment Method")).toBeNull();
  });
});

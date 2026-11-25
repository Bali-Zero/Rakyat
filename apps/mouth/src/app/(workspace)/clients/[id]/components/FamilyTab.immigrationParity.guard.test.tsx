/**
 * Guard: a family member's row shows the same immigration facts the main
 * client's Immigration tab shows — permit type, permit expiry date, passport
 * expiry date — and replacing a stay permit never removes the old one.
 * Synthetic fixtures only: placeholder names, `X0000000`, no client data.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FamilyTab } from "./FamilyTab";
import { api } from "@/lib/api";
import type { ClientDocument, FamilyMember } from "@/lib/api/crm/crm.types";

vi.mock("@/lib/api", () => ({
  api: {
    crm: { deleteFamilyMember: vi.fn(), deleteDocument: vi.fn() },
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus: vi.fn() }),
}));

vi.mock("@/lib/utils", async (orig) => ({
  ...(await orig<typeof import("@/lib/utils")>()),
  fileToBase64: vi.fn(async () => "c3ludGhldGlj"),
}));

const DAY = "2026-11-25";

/** A member with nothing typed on the row: every fact comes from documents. */
const BARE: FamilyMember = {
  id: 1,
  client_id: 900,
  full_name: "Placeholder Spouse",
  relationship: "spouse",
  nationality: "Placeholderland",
} as FamilyMember;

const STAY_PERMIT: ClientDocument = {
  id: 50,
  client_id: 900,
  family_member_id: 1,
  document_type: "visa",
  document_category: "immigration",
  issue_date: "2026-07-01",
  expiry_date: "2027-06-30",
  permit_code: "E31A",
  permit_label: "E31A — Spouse KITAS",
  created_at: "2026-07-02T03:00:00Z",
  google_drive_file_url:
    "https://drive.google.com/file/d/synthetic-permit/view",
} as ClientDocument;

const PASSPORT: ClientDocument = {
  id: 51,
  client_id: 900,
  family_member_id: 1,
  document_type: "passport",
  expiry_date: "2029-03-12",
  created_at: "2026-07-02T03:00:00Z",
  google_drive_file_url:
    "https://drive.google.com/file/d/synthetic-passport/view",
} as ClientDocument;

const renderTab = (
  familyMembers: FamilyMember[],
  documents: ClientDocument[] = [],
) =>
  render(
    <FamilyTab
      clientId={900}
      familyMembers={familyMembers}
      documents={documents}
      formatDate={(date) => `formatted:${date}`}
      onAddClick={() => {}}
      onEditClick={() => {}}
      onRefresh={() => {}}
      day={DAY}
    />,
  );

beforeEach(() => vi.clearAllMocks());

describe("FamilyTab — immigration parity with the main client", () => {
  it("GUILT: a member whose permit and passport are only on documents shows the permit type, its expiry and the passport expiry", () => {
    renderTab([BARE], [STAY_PERMIT, PASSPORT]);
    const row = within(screen.getByTestId("family-member-row-1"));

    // Same `<code> · <name>` the Immigration tab's current-permit panel shows.
    expect(row.getByText("E31A · Spouse KITAS")).toBeInTheDocument();
    expect(row.getByText("formatted:2027-06-30")).toBeInTheDocument();
    expect(row.getByText("formatted:2029-03-12")).toBeInTheDocument();
    expect(row.getByText("Valid")).toBeInTheDocument();
    expect(row.queryByText("No visa")).not.toBeInTheDocument();
    expect(row.queryByText("No visa on file")).not.toBeInTheDocument();
  });

  it("GUILT: the passport typed on the member shows its expiry date, not only the time left", () => {
    renderTab([
      { ...BARE, passport_number: "X0000000", passport_expiry: "2029-03-12" },
    ]);
    const row = within(screen.getByTestId("family-member-row-1"));

    expect(row.getByText("formatted:2029-03-12")).toBeInTheDocument();
    expect(row.getByText(/^Passport: /)).toBeInTheDocument();
  });

  it("GUILT: a permit document on file with nothing extracted is 'Visa on file', never 'No visa'", () => {
    renderTab(
      [BARE],
      [
        {
          ...STAY_PERMIT,
          issue_date: undefined,
          expiry_date: undefined,
          permit_code: undefined,
          permit_label: undefined,
        } as ClientDocument,
      ],
    );
    const row = within(screen.getByTestId("family-member-row-1"));

    expect(row.getByText("Visa on file")).toBeInTheDocument();
    expect(row.queryByText("No visa")).not.toBeInTheDocument();
    // Still honest: no type and no date are invented.
    expect(row.queryByText(/formatted:/)).not.toBeInTheDocument();
    // The raw `document_type` is never shown as a type ("Visa" is only the
    // file-group label in the actions cell).
    expect(row.queryByText("visa")).not.toBeInTheDocument();
    expect(
      row.getByText("Document on file — upload to extract data via OCR"),
    ).toBeInTheDocument();
  });

  it("INNOCENCE: a member with no visa and no passport data stays empty — no invented date", () => {
    renderTab([BARE]);
    const row = within(screen.getByTestId("family-member-row-1"));

    expect(row.getByText("No visa")).toBeInTheDocument();
    expect(row.getByText("No visa on file")).toBeInTheDocument();
    expect(row.queryByText(/formatted:/)).not.toBeInTheDocument();
    expect(row.queryByText(/Passport:/)).not.toBeInTheDocument();
    expect(
      row.queryByRole("button", { name: /Replace stay permit/ }),
    ).not.toBeInTheDocument();
  });

  it("INNOCENCE: the member's own typed fields still win over documents (the shared family rule)", () => {
    renderTab(
      [{ ...BARE, current_visa_type: "ITK", visa_expiry: "2026-12-20" }],
      [STAY_PERMIT],
    );
    const row = within(screen.getByTestId("family-member-row-1"));

    expect(row.getByText("ITK")).toBeInTheDocument();
    expect(row.getByText("formatted:2026-12-20")).toBeInTheDocument();
  });

  it("GUILT: the passport and visa file controls are labelled groups", () => {
    renderTab([BARE], [STAY_PERMIT, PASSPORT]);
    const row = within(screen.getByTestId("family-member-row-1"));

    const passportGroup = row.getByRole("group", { name: "Passport" });
    const visaGroup = row.getByRole("group", { name: "Visa" });
    expect(within(passportGroup).getByText("Passport")).toBeVisible();
    expect(within(visaGroup).getByText("Visa")).toBeVisible();
    expect(
      within(passportGroup).getByRole("button", {
        name: "View passport for Placeholder Spouse",
      }),
    ).toBeInTheDocument();
    expect(
      within(visaGroup).getByRole("button", {
        name: "View visa for Placeholder Spouse",
      }),
    ).toBeInTheDocument();
  });

  it("GUILT: replacing the stay permit uploads a new permit for the member and never deletes or archives the old one", async () => {
    vi.mocked(api.post).mockResolvedValueOnce({ success: true });
    renderTab([BARE], [STAY_PERMIT, PASSPORT]);

    const replace = screen.getByRole("button", {
      name: "Replace stay permit for Placeholder Spouse",
    });
    expect(replace).toHaveTextContent("Replace stay permit");
    const input = replace.previousElementSibling as HTMLInputElement;
    fireEvent.change(input, {
      target: {
        files: [new File(["x"], "new-permit.pdf", { type: "application/pdf" })],
      },
    });

    await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith(
      "/api/crm/clients/900/documents/upload",
      expect.objectContaining({
        family_member_id: 1,
        document_type: "visa",
        file_name: "new-permit.pdf",
      }),
    );
    expect(api.crm.deleteDocument).not.toHaveBeenCalled();
    expect(api.delete).not.toHaveBeenCalled();
    expect(api.patch).not.toHaveBeenCalled();
  });
});

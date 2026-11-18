import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type {
  ClientCompanyLink,
  ClientDocument,
} from "@/lib/api/crm/crm.types";
import { DocumentsTab } from "./DocumentsTab";

// Synthetic fixtures only (FOLDER_123, COMPANY_9): no real names or ids.

vi.mock("@/lib/api", () => ({ api: { crm: { deleteDocument: vi.fn() } } }));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  }),
}));
// The Drive panel has its own tests (TaxDocumentsSection.test.tsx); here we
// only care whether the Documents tab mounts it, and with what.
const taxSection = vi.fn();
vi.mock("./TaxDocumentsSection", () => ({
  TaxDocumentsSection: (props: unknown) => {
    taxSection(props);
    return <div data-testid="tax-documents-section" />;
  },
}));

const doc = (id: number, category: string): ClientDocument =>
  ({
    id,
    client_id: 1,
    document_type: `${category}_doc`,
    document_category: category,
    file_name: `doc-${id}.pdf`,
    status: "verified",
    expiry_date: undefined,
    created_at: "2026-01-01",
  }) as ClientDocument;

const tax = [doc(1, "tax"), doc(2, "tax"), doc(3, "tax")];
const personal = [doc(4, "personal")];
const other = [doc(5, "other")];
const links = [
  { link_id: 9, company_id: 9, company_name: "COMPANY_9" },
] as ClientCompanyLink[];

function renderTab() {
  render(
    <DocumentsTab
      clientId={1}
      documents={[...tax, ...personal, ...other]}
      documentsByCategory={{ tax, personal, other }}
      clientFolderId="FOLDER_123"
      companyLinks={links}
      formatDate={(d) => d}
      onAddClick={() => {}}
      onEditClick={() => {}}
      onRefresh={() => {}}
      day="2026-11-18"
    />,
  );
}

const pill = (name: RegExp) =>
  fireEvent.click(screen.getByRole("button", { name }));
const rowCount = () =>
  screen.queryAllByRole("button", { name: /^Edit / }).length;

describe("DocumentsTab — Tax filter shows the Drive tax folders", () => {
  it("GUILT: the Tax filter mounts Tax Documents with the client folder and companies", () => {
    renderTab();
    pill(/^✓?Tax · 3/);
    expect(screen.getByTestId("tax-documents-section")).toBeTruthy();
    expect(taxSection).toHaveBeenLastCalledWith({
      clientFolderId: "FOLDER_123",
      companyLinks: links,
    });
  });

  it.each([/^✓?All · /, /^✓?Personal · /, /^✓?Other · /])(
    "INNOCENCE: %s does not mount it",
    (name) => {
      renderTab();
      pill(/^✓?Tax · 3/);
      pill(name);
      expect(screen.queryByTestId("tax-documents-section")).toBeNull();
    },
  );

  it("INNOCENCE: the default (All) view does not mount it", () => {
    taxSection.mockClear();
    renderTab();
    expect(screen.queryByTestId("tax-documents-section")).toBeNull();
    expect(taxSection).not.toHaveBeenCalled();
  });
});

describe("DocumentsTab — a filter's count equals the rows it lists", () => {
  it.each([
    [/^✓?Tax · 3/, 3],
    [/^✓?Personal · 1/, 1],
    [/^✓?Other · 1/, 1],
    [/^✓?All · 5/, 5],
  ])("%s lists %i rows", (name, n) => {
    renderTab();
    pill(name);
    expect(rowCount()).toBe(n);
  });
});

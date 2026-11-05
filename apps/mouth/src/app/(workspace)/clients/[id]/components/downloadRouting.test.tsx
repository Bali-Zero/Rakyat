import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ClientDocument, ClientProfile } from "@/lib/api/crm/crm.types";
import { PassportCard } from "./PassportCard";
import { VisaCard } from "./VisaCard";
import { downloadHrefFor, isRejectedDocument } from "./utils";

vi.mock("@/lib/api", () => ({
  api: {
    crm: { extractPassportForClient: vi.fn() },
    post: vi.fn(),
    delete: vi.fn(),
  },
}));
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  },
}));
vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus: vi.fn() }),
}));

const DRIVE = "https://drive.google.com/file/d/drive-file-123/view";
const FOREIGN_ID = "https://files.example.test/download?id=42";

const hrefs: (string | null)[] = [];
const spyClick = () =>
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    hrefs.push(this.getAttribute("href"));
  });

afterEach(() => {
  vi.restoreAllMocks();
  hrefs.length = 0;
});

const visaDocs = (url: string) =>
  [
    {
      id: 9,
      client_id: 42,
      document_type: "kitas",
      document_category: "immigration",
      file_name: "kitas.pdf",
      google_drive_file_url: url,
      created_at: "2026-10-01T00:00:00Z",
    },
  ] as ClientDocument[];

const passportDocs = (url: string) =>
  [
    {
      id: 7,
      client_id: 42,
      document_type: "passport",
      document_category: "personal",
      file_name: "passport.jpg",
      google_drive_file_url: url,
    },
  ] as ClientDocument[];

const renderVisa = (url: string) =>
  render(
    <VisaCard
      client={{ id: 42, full_name: "Sample Client" } as ClientProfile["client"]}
      clientId={42}
      documents={visaDocs(url)}
      activePractices={[]}
      formatDate={(v) => v}
      formatCurrency={String}
      onRefresh={vi.fn()}
    />,
  );

const renderPassport = (url: string) =>
  render(
    <PassportCard
      client={{ id: 42, full_name: "Test Client" } as never}
      documents={passportDocs(url)}
      formatDate={(v) => v}
      onRefresh={vi.fn()}
      clientId={42}
    />,
  );

describe("download routing: proxy only for Drive hosts", () => {
  it("GUILT visa: a foreign host with ?id= downloads as itself, not via the proxy", () => {
    spyClick();
    renderVisa(FOREIGN_ID);
    fireEvent.click(screen.getByRole("button", { name: "Download visa" }));
    expect(hrefs).toEqual([FOREIGN_ID]);
  });

  it("INNOCENCE visa: a Drive URL goes through the proxy", () => {
    spyClick();
    renderVisa(DRIVE);
    fireEvent.click(screen.getByRole("button", { name: "Download visa" }));
    expect(hrefs).toEqual(["/api/documents/proxy/drive-file-123"]);
  });

  it("GUILT passport: a foreign host with ?id= downloads as itself, not via the proxy", () => {
    spyClick();
    renderPassport(FOREIGN_ID);
    fireEvent.click(screen.getByRole("button", { name: /download/i }));
    expect(hrefs).toEqual([FOREIGN_ID]);
  });

  it("INNOCENCE passport: a Drive URL goes through the proxy", () => {
    spyClick();
    renderPassport(DRIVE);
    fireEvent.click(screen.getByRole("button", { name: /download/i }));
    expect(hrefs).toEqual(["/api/documents/proxy/drive-file-123"]);
  });

  it("a lookalike host is not Drive, and javascript: yields nothing", () => {
    expect(
      downloadHrefFor("https://drive.google.com.example.test/file/d/x/view"),
    ).toBe("https://drive.google.com.example.test/file/d/x/view");
    expect(downloadHrefFor("javascript:alert(1)")).toBeNull();
  });
});

describe("isRejectedDocument", () => {
  it("matches case- and whitespace-insensitively, nothing else", () => {
    expect(isRejectedDocument({ status: " Rejected " })).toBe(true);
    expect(isRejectedDocument({ status: "verified" })).toBe(false);
    expect(isRejectedDocument({})).toBe(false);
  });
});

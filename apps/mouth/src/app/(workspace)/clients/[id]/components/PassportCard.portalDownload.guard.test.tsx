/**
 * Guard — the Passport card can download a passport the client uploaded
 * through the portal.
 *
 * Portal uploads store `file_id` + `file_url` and never
 * `google_drive_file_url`. The card read only `google_drive_file_url`, so after
 * staff retyped a portal upload to "passport" the card said "Scan on file" with
 * Download disabled (client 12737, 2026-11-13). The Documents tab already opens
 * these rows through `getDocumentOpenUrl`; the card now uses the same rule.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { PassportCard } from "./PassportCard";

vi.mock("@/lib/api", () => ({
  api: {
    crm: { extractPassportForClient: vi.fn() },
    post: vi.fn(),
    request: vi.fn(),
  },
}));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  }),
}));

const CLIENT_ID = 12737;

const passport = (over: Partial<ClientDocument>): ClientDocument[] =>
  [
    {
      id: 501,
      client_id: CLIENT_ID,
      document_type: "passport",
      document_category: "personal",
      file_name: "Passport",
      status: "processing",
      ...over,
    },
  ] as ClientDocument[];

let hrefs: string[] = [];
const spyClick = () => {
  hrefs = [];
  return vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      hrefs.push(this.getAttribute("href") ?? "");
    });
};
afterEach(() => vi.restoreAllMocks());

const renderCard = (documents: ClientDocument[]) =>
  render(
    <PassportCard
      client={{ id: CLIENT_ID, full_name: "Test Client" } as never}
      documents={documents}
      formatDate={(v) => v}
      onRefresh={vi.fn()}
      clientId={CLIENT_ID}
    />,
  );

describe("Passport card Download for a portal upload", () => {
  it("GUILT: a portal upload (file_id + file_url, no google_drive_file_url) downloads through the proxy", () => {
    spyClick();
    renderCard(
      passport({
        file_id: "portal-file-abc",
        file_url: "https://storage.example.test/portal-file-abc",
      }),
    );
    expect(screen.getByText(/Scan on file/)).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "Download passport" });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);
    expect(hrefs).toEqual(["/api/documents/proxy/portal-file-abc"]);
  });

  it("INNOCENCE: a staff upload with a Drive URL still goes through the proxy", () => {
    spyClick();
    renderCard(
      passport({
        status: "verified",
        google_drive_file_url:
          "https://drive.google.com/file/d/drive-file-123/view",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Download passport" }));
    expect(hrefs).toEqual(["/api/documents/proxy/drive-file-123"]);
  });

  it("INNOCENCE: a row with no file at all keeps Download disabled", () => {
    renderCard(passport({}));
    expect(
      screen.getByRole("button", { name: "Download passport" }),
    ).toBeDisabled();
  });
});

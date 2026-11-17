/**
 * Guard — the Passport card offers Extract and Delete for a passport the
 * client uploaded through the portal.
 *
 * Portal uploads store `file_id` + `file_url` and never
 * `google_drive_file_url`. Download was fixed to use `getDocumentOpenUrl`, but
 * Extract and Delete were still gated on `google_drive_file_url`, so a portal
 * passport showed neither (client 12737, doc 21026, 2026-11-13). Extract reads
 * the scan through the same proxy as Download; Delete is by document id and
 * keeps its confirmation toast and soft delete.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { api } from "@/lib/api";
import { toast } from "sonner";
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

const portalPassport = () =>
  passport({
    file_id: "portal-file-abc",
    file_url: "https://storage.example.test/portal-file-abc",
  });

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

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

describe("Passport card Extract and Delete for a portal upload", () => {
  it("GUILT: a portal upload (file_id + file_url, no google_drive_file_url) shows Extract and Delete, enabled", () => {
    renderCard(portalPassport());
    expect(screen.getByRole("button", { name: /Extract/ })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Delete passport" }),
    ).toBeEnabled();
  });

  it("GUILT: Extract fetches the portal file through the proxy and sends it to OCR", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["synthetic"], { type: "image/jpeg" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.mocked(api.crm.extractPassportForClient).mockResolvedValue({
      success: true,
    } as never);

    renderCard(portalPassport());
    fireEvent.click(screen.getByRole("button", { name: /Extract/ }));

    await waitFor(() =>
      expect(api.crm.extractPassportForClient).toHaveBeenCalledTimes(1),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/documents/proxy/portal-file-abc",
    );
    expect(vi.mocked(api.crm.extractPassportForClient).mock.calls[0][1]).toBe(
      "image/jpeg",
    );
    expect(vi.mocked(api.crm.extractPassportForClient).mock.calls[0][2]).toBe(
      CLIENT_ID,
    );
  });

  it("GUILT: Delete asks for confirmation, then soft-deletes by document id", async () => {
    renderCard(portalPassport());
    fireEvent.click(screen.getByRole("button", { name: "Delete passport" }));

    // Nothing is deleted until the confirmation toast's action runs.
    expect(api.request).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledTimes(1);
    const opts = vi.mocked(toast).mock.calls[0][1] as unknown as {
      action: { onClick: () => void };
    };
    opts.action.onClick();

    await waitFor(() => expect(api.request).toHaveBeenCalledTimes(1));
    expect(api.request).toHaveBeenCalledWith("/api/crm/documents/501", {
      method: "DELETE",
    });
  });

  it("INNOCENCE: a staff upload with a Drive URL still extracts through the Drive proxy", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["synthetic"], { type: "image/png" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.mocked(api.crm.extractPassportForClient).mockResolvedValue({
      success: true,
    } as never);

    renderCard(
      passport({
        status: "verified",
        google_drive_file_url:
          "https://drive.google.com/file/d/drive-file-123/view",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /Extract/ }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/documents/proxy/drive-file-123",
      ),
    );
  });

  it("INNOCENCE: a row with no file at all offers no Extract or Delete", () => {
    renderCard(passport({}));
    expect(screen.queryByRole("button", { name: /Extract/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Delete passport" }),
    ).toBeNull();
  });
});

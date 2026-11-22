import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CompanyDocument } from "@/lib/api/crm/crm.types";

const mocks = vi.hoisted(() => ({
  post: vi.fn(),
  pollOcrStatus: vi.fn(),
  onDone: undefined as undefined | (() => void),
}));

vi.mock("@/lib/api", () => ({ api: { post: mocks.post } }));
vi.mock("@/lib/utils", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  fileToBase64: vi.fn().mockResolvedValue("ZmFrZQ=="),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: (opts: { onDone: () => void }) => {
    mocks.onDone = opts.onDone;
    return { ocrPolling: false, pollOcrStatus: mocks.pollOcrStatus };
  },
}));

import { CompanyDocUpload } from "./CompanyDocUpload";

function doc(fileId?: string): CompanyDocument {
  return {
    id: 900001,
    uuid: "u-900001",
    document_type: "nib",
    status: "active",
    google_drive_file_id: fileId,
    file_name: "old-nib.pdf",
    is_verified: false,
    created_at: "2026-01-01",
  };
}

function renderUpload(
  existingDoc: CompanyDocument | null,
  onUploaded = vi.fn(),
) {
  const props = {
    clientId: 900001,
    companyId: 900002,
    companyName: "PT Contoh Satu",
    docType: "nib",
    label: "NIB",
    hint: "Business ID",
  };
  const view = render(
    <CompanyDocUpload
      {...props}
      existingDoc={existingDoc}
      onUploaded={onUploaded}
    />,
  );
  const rerenderWith = (next: CompanyDocument | null) =>
    view.rerender(
      <CompanyDocUpload
        {...props}
        existingDoc={next}
        onUploaded={onUploaded}
      />,
    );
  return { ...view, onUploaded, rerenderWith };
}

async function pickFile() {
  const input = screen.getByLabelText("Upload company document");
  const file = new File(["x"], "new-nib.pdf", { type: "application/pdf" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
}

beforeEach(() => {
  mocks.post.mockReset();
  mocks.pollOcrStatus.mockReset();
  mocks.onDone = undefined;
});
afterEach(cleanup);

describe("CompanyDocUpload", () => {
  it("tells the parent at once after a successful upload, even if unmounted before any poll result", async () => {
    mocks.post.mockResolvedValue({ success: true });
    const { onUploaded, unmount } = renderUpload(null);
    await pickFile();
    unmount();
    expect(mocks.pollOcrStatus).toHaveBeenCalledTimes(1);
    expect(onUploaded).toHaveBeenCalledTimes(1);
  });

  it("does not tell the parent when the upload fails", async () => {
    mocks.post.mockResolvedValue({ success: false, message: "no" });
    const { onUploaded } = renderUpload(null);
    await pickFile();
    expect(onUploaded).not.toHaveBeenCalled();
    expect(mocks.pollOcrStatus).not.toHaveBeenCalled();
  });

  it("still tells the parent when the polling finishes", async () => {
    mocks.post.mockResolvedValue({ success: true });
    const { onUploaded } = renderUpload(null);
    await pickFile();
    onUploaded.mockClear();
    mocks.onDone?.();
    expect(onUploaded).toHaveBeenCalledTimes(1);
  });

  it("shows View and Download for an existing document when nothing was uploaded", () => {
    renderUpload(doc("drive-old"));
    expect(screen.getByRole("button", { name: /View/ })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Download/ }),
    ).toBeInTheDocument();
  });

  it("keeps the buttons away while the previous file is still the one passed", async () => {
    mocks.post.mockResolvedValue({ success: true });
    const { rerenderWith } = renderUpload(doc("drive-old"));
    await pickFile();
    expect(screen.queryByRole("button", { name: /View/ })).toBeNull();
    rerenderWith(doc("drive-old"));
    expect(screen.queryByRole("button", { name: /View/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Download/ })).toBeNull();
  });

  it("shows the buttons for the new file id and opens that one", async () => {
    mocks.post.mockResolvedValue({ success: true });
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { rerenderWith } = renderUpload(doc("drive-old"));
    await pickFile();
    rerenderWith(doc("drive-new"));
    fireEvent.click(screen.getByRole("button", { name: /View/ }));
    expect(open).toHaveBeenCalledWith(
      "/api/documents/proxy/drive-new",
      "_blank",
    );
    open.mockRestore();
  });

  it("treats any file id as new when there was no document before the upload", async () => {
    mocks.post.mockResolvedValue({ success: true });
    const { rerenderWith } = renderUpload(null);
    await pickFile();
    expect(screen.queryByRole("button", { name: /View/ })).toBeNull();
    rerenderWith(doc("drive-first"));
    expect(screen.getByRole("button", { name: /View/ })).toBeInTheDocument();
  });
});

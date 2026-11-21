import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  FilesBrowserPanel,
  driveProxyHref,
  type FilesBrowserActions,
} from "./FilesBrowserPanel";
import { api } from "@/lib/api";
import type { FileItem } from "@/lib/api/drive/drive.types";

vi.mock("@/lib/api", () => ({
  api: {
    drive: {
      listFiles: vi.fn(),
    },
  },
}));

const { mockToastSuccess, mockToastError } = vi.hoisted(() => ({
  mockToastSuccess: vi.fn(),
  mockToastError: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: {
    success: mockToastSuccess,
    error: mockToastError,
  },
}));

const listFiles = vi.mocked(api.drive.listFiles);

function file(partial: Partial<FileItem> & { id: string; name: string }) {
  return {
    mime_type: "application/pdf",
    is_folder: false,
    ...partial,
  } as FileItem;
}

beforeEach(() => {
  listFiles.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
});

describe("FilesBrowserPanel", () => {
  it("lists the client's own folder, folders first", async () => {
    listFiles.mockResolvedValue({
      files: [
        file({ id: "f-b", name: "b-passport.pdf" }),
        file({ id: "d-a", name: "Tax", is_folder: true }),
      ],
      breadcrumb: [],
    } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" />);

    await screen.findByText("b-passport.pdf");
    expect(listFiles).toHaveBeenCalledWith({
      folder_id: "FOLDER_123",
      page_size: 100,
    });
    const names = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(names[0]).toContain("Tax");
    expect(names[1]).toContain("b-passport.pdf");
  });

  it("opens a file through the kita proxy, never a bare Drive URL", async () => {
    listFiles.mockResolvedValue({
      files: [file({ id: "FILE_9", name: "npwp.pdf" })],
      breadcrumb: [],
    } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" />);

    const link = await screen.findByRole("link", { name: /npwp\.pdf/ });
    expect(link.getAttribute("href")).toBe("/api/documents/proxy/FILE_9");
    expect(link.getAttribute("href")).toBe(driveProxyHref("FILE_9"));
    expect(link.getAttribute("href")).not.toContain("drive.google.com");
  });

  it("enters a subfolder and comes back", async () => {
    listFiles.mockImplementation(async (params) => {
      if (params?.folder_id === "SUB_1") {
        return {
          files: [file({ id: "f-in", name: "inside.pdf" })],
          breadcrumb: [],
        } as never;
      }
      return {
        files: [file({ id: "SUB_1", name: "Setup", is_folder: true })],
        breadcrumb: [],
      } as never;
    });

    render(<FilesBrowserPanel folderId="FOLDER_123" />);

    fireEvent.click(await screen.findByRole("button", { name: /Setup/ }));
    await screen.findByText("inside.pdf");
    expect(listFiles).toHaveBeenLastCalledWith({
      folder_id: "SUB_1",
      page_size: 100,
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Back to parent folder" }),
    );
    await screen.findByRole("button", { name: /Setup/ });
    expect(listFiles).toHaveBeenLastCalledWith({
      folder_id: "FOLDER_123",
      page_size: 100,
    });
  });

  it("shows an error instead of an empty folder when listing fails", async () => {
    listFiles.mockRejectedValue(new Error("403"));

    render(<FilesBrowserPanel folderId="FOLDER_123" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the Drive folder.",
    );
    expect(screen.queryByText("This folder is empty.")).toBeNull();
  });

  it("says so when the folder is empty", async () => {
    listFiles.mockResolvedValue({ files: [], breadcrumb: [] } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" />);

    await waitFor(() =>
      expect(screen.getByText("This folder is empty.")).toBeInTheDocument(),
    );
  });
});

describe("FilesBrowserPanel lazy listing", () => {
  it("does not list the Drive folder until the panel nears the viewport", async () => {
    let callback: IntersectionObserverCallback | null = null;
    vi.stubGlobal(
      "IntersectionObserver",
      vi.fn(function (this: unknown, cb: IntersectionObserverCallback) {
        callback = cb;
        return { observe: vi.fn(), disconnect: vi.fn(), unobserve: vi.fn() };
      }),
    );
    try {
      listFiles.mockResolvedValue({
        files: [file({ id: "f-1", name: "kitas.pdf" })],
        breadcrumb: [],
      } as never);
      render(<FilesBrowserPanel folderId="FOLDER_123" />);
      expect(listFiles).not.toHaveBeenCalled();

      callback!(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      );
      await screen.findByText("kitas.pdf");
      expect(listFiles).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

function makeActions(): FilesBrowserActions & {
  onUpload: ReturnType<typeof vi.fn>;
  onRename: ReturnType<typeof vi.fn>;
  onReplace: ReturnType<typeof vi.fn>;
  onDelete: ReturnType<typeof vi.fn>;
} {
  return {
    onUpload: vi.fn().mockResolvedValue({ success: true }),
    onRename: vi.fn().mockResolvedValue({ success: true }),
    onReplace: vi.fn().mockResolvedValue({ success: true }),
    onDelete: vi.fn().mockResolvedValue({ success: true }),
  };
}

describe("FilesBrowserPanel write actions (upload / edit / delete)", () => {
  it("renders no Upload, Edit, or Delete controls when actions are omitted", async () => {
    listFiles.mockResolvedValue({
      files: [file({ id: "FILE_1", name: "spt.pdf" })],
      breadcrumb: [],
    } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" />);
    await screen.findByText("spt.pdf");

    expect(screen.queryByRole("button", { name: /^Upload$/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Edit / })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Delete / })).toBeNull();
  });

  it("uploads into the currently open subfolder and refreshes the listing", async () => {
    const actions = makeActions();
    listFiles.mockImplementation(async (params) => {
      if (params?.folder_id === "SUB_2026") {
        return {
          files: [file({ id: "FILE_OLD", name: "old.pdf" })],
          breadcrumb: [],
        } as never;
      }
      return {
        files: [file({ id: "SUB_2026", name: "2026", is_folder: true })],
        breadcrumb: [],
      } as never;
    });

    render(<FilesBrowserPanel folderId="FOLDER_123" actions={actions} />);
    fireEvent.click(await screen.findByRole("button", { name: /2026/ }));
    await screen.findByText("old.pdf");

    const input = screen.getByLabelText("Upload document to 2026");
    const upload = new File(["%PDF-1.4"], "spt-2026.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(input, { target: { files: [upload] } });

    await waitFor(() =>
      expect(actions.onUpload).toHaveBeenCalledWith("SUB_2026", upload),
    );
    await waitFor(() => expect(listFiles).toHaveBeenCalledTimes(3));
    expect(mockToastSuccess).toHaveBeenCalledWith('Uploaded "spt-2026.pdf"');
  });

  it("rejects empty and oversized uploads before calling onUpload", async () => {
    const actions = {
      ...makeActions(),
      maxUploadBytes: 10,
      maxUploadLabel: "10 B",
    };
    listFiles.mockResolvedValue({ files: [], breadcrumb: [] } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" actions={actions} />);
    await screen.findByText("This folder is empty.");

    const input = screen.getByLabelText("Upload document to Client folder");
    fireEvent.change(input, {
      target: {
        files: [new File([], "empty.pdf", { type: "application/pdf" })],
      },
    });
    fireEvent.change(input, {
      target: {
        files: [
          new File(["12345678901"], "big.pdf", { type: "application/pdf" }),
        ],
      },
    });

    expect(actions.onUpload).not.toHaveBeenCalled();
    expect(mockToastError).toHaveBeenCalledTimes(2);
  });

  it("shows Edit and Delete only on files (never folders) and renames with IME-safe Enter", async () => {
    const actions = makeActions();
    listFiles.mockResolvedValue({
      files: [
        file({ id: "DIR_1", name: "SPT", is_folder: true }),
        file({ id: "FILE_1", name: "draft.pdf" }),
      ],
      breadcrumb: [],
    } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" actions={actions} />);
    await screen.findByText("draft.pdf");

    expect(screen.queryByRole("button", { name: "Edit SPT" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete SPT" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Edit draft.pdf" }));
    const nameInput = screen.getByLabelText("File name");
    fireEvent.change(nameInput, { target: { value: "final-2025.pdf" } });

    // IME composing Enter (keyCode 229) must NOT submit prematurely
    fireEvent.keyDown(nameInput, { key: "Enter", keyCode: 229 });
    expect(actions.onRename).not.toHaveBeenCalled();

    // Regular Enter submits
    fireEvent.keyDown(nameInput, { key: "Enter", keyCode: 13 });
    await waitFor(() =>
      expect(actions.onRename).toHaveBeenCalledWith(
        expect.objectContaining({ id: "FILE_1", name: "draft.pdf" }),
        "final-2025.pdf",
      ),
    );
    await waitFor(() => expect(listFiles).toHaveBeenCalledTimes(2));
  });

  it("replaces file content in-place through the Edit dialog", async () => {
    const actions = makeActions();
    listFiles.mockResolvedValue({
      files: [file({ id: "FILE_1", name: "old.pdf" })],
      breadcrumb: [],
    } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" actions={actions} />);
    await screen.findByText("old.pdf");

    fireEvent.click(screen.getByRole("button", { name: "Edit old.pdf" }));
    const replaceInput = screen.getByLabelText("Replace file (optional)");
    const replacement = new File(["%PDF-1.4 v2"], "updated.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(replaceInput, { target: { files: [replacement] } });

    // Untouched name automatically picks up replacement filename
    expect((screen.getByLabelText("File name") as HTMLInputElement).value).toBe(
      "updated.pdf",
    );

    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(actions.onReplace).toHaveBeenCalledWith(
        expect.objectContaining({ id: "FILE_1" }),
        replacement,
        "updated.pdf",
      ),
    );
    expect(actions.onRename).not.toHaveBeenCalled();
  });

  it("confirms deletion before calling onDelete and skips when cancelled", async () => {
    const actions = makeActions();
    listFiles.mockResolvedValue({
      files: [file({ id: "FILE_1", name: "wrong.pdf" })],
      breadcrumb: [],
    } as never);

    render(<FilesBrowserPanel folderId="FOLDER_123" actions={actions} />);
    await screen.findByText("wrong.pdf");

    fireEvent.click(screen.getByRole("button", { name: "Delete wrong.pdf" }));
    expect(screen.getByText("Delete document?")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(actions.onDelete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Delete wrong.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: /^Delete$/ }));
    await waitFor(() =>
      expect(actions.onDelete).toHaveBeenCalledWith(
        expect.objectContaining({ id: "FILE_1", name: "wrong.pdf" }),
      ),
    );
    await waitFor(() => expect(listFiles).toHaveBeenCalledTimes(2));
  });
});

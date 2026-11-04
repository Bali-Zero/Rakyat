import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FilesBrowserPanel, driveProxyHref } from "./FilesBrowserPanel";
import { api } from "@/lib/api";
import type { FileItem } from "@/lib/api/drive/drive.types";

vi.mock("@/lib/api", () => ({
  api: {
    drive: {
      listFiles: vi.fn(),
    },
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

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UploadFlow } from "./UploadFlow";
import { messageFor } from "./messages";
import type { UploadErrorCode } from "./types";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function errorBody(code: string, retryable: boolean) {
  return {
    code,
    retryable,
    message_key: `garuda_voa.error.${code.toLowerCase()}`,
  };
}

describe("UploadFlow error step", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    global.fetch = mockFetch;
    mockFetch.mockReset();
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function reachError(status: number, code: string, retryable: boolean) {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(status, errorBody(code, retryable)),
    );
    render(<UploadFlow resultId="result-1" />);
    const input = screen.getByLabelText("Upload passport photo");
    const file = new File([new Uint8Array(1024)], "passport.jpg", {
      type: "image/jpeg",
    });
    await userEvent.upload(input, file);
    await screen.findByText(messageFor(code as UploadErrorCode));
  }

  it("offers a way to choose another photo after a non-retryable file error", async () => {
    await reachError(413, "DOCUMENT_TOO_LARGE", false);

    const button = await screen.findByRole("button", {
      name: "Choose another photo",
    });
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();

    const clickSpy = vi.spyOn(HTMLInputElement.prototype, "click");
    await userEvent.click(button);

    expect(clickSpy).toHaveBeenCalled();
    expect(
      await screen.findByRole("button", { name: "Take or choose a photo" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Choose another photo" }),
    ).toBeNull();
  });

  it.each(["SESSION_REQUIRED", "RESULT_NOT_FOUND"])(
    "links back to where a check starts after a non-retryable %s",
    async (code) => {
      await reachError(code === "SESSION_REQUIRED" ? 401 : 404, code, false);

      const link = await screen.findByRole("link", { name: /Start again/ });
      expect(link.getAttribute("href")).toBe("/visa/voa");
      expect(
        screen.queryByRole("button", { name: "Choose another photo" }),
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    },
  );

  it("shows Try again and nothing new for a retryable error", async () => {
    await reachError(503, "INTERNAL_ERROR", true);

    expect(
      await screen.findByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Choose another photo" }),
    ).toBeNull();
    expect(screen.queryByRole("link", { name: /Start again/ })).toBeNull();
  });

  it("keeps the store-down error exactly as it was", async () => {
    await reachError(503, "PERSISTENCE_POLICY_UNAVAILABLE", false);

    expect(
      screen.getAllByRole("link", { name: /type your passport details/i }),
    ).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: "Choose another photo" }),
    ).toBeNull();
    expect(screen.queryByRole("link", { name: /Start again/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});

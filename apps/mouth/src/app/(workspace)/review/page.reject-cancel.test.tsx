/**
 * Rejecting a document is final. The page asks for a reason with the browser's prompt;
 * Cancel there means the reviewer backed out, and nothing may be rejected.
 * Invented values only.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewPage from "./page";

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  getToken: vi.fn(() => null),
  getProfile: vi.fn(),
  getUserProfile: vi.fn(),
  crm: { createClient: vi.fn() },
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));

vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

const PROPOSAL = {
  proposal_id: 1,
  doc_type: "passport",
  decision: "AUTO_ATTACH",
  source: "whatsapp",
  status: "review_pending",
  received_by: "reviewer.one@example.test",
  entity_candidates: [{ client_id: 900021, full_name: "Zz Client One" }],
  extracted_fields: {},
  created_at: "2026-06-15T09:00:00Z",
  routing: {},
};

const rejectCalls = () =>
  apiMock.post.mock.calls.filter(([endpoint]) =>
    String(endpoint).endsWith("/reject"),
  );

async function openTheDocument() {
  render(<ReviewPage />);
  fireEvent.click(
    (await screen.findAllByRole("button", { name: "Review" }))[0],
  );
  const reject = await screen.findByRole("button", { name: "Reject" });
  await waitFor(() => expect(reject).toBeEnabled());
  return reject;
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.getUserProfile.mockReturnValue({
    email: "reviewer.one@example.test",
    role: "admin",
  });
  apiMock.get.mockImplementation(async (endpoint: string) => {
    if (endpoint.startsWith("/api/intake/review/queue")) {
      return { items: [PROPOSAL] };
    }
    if (endpoint === "/api/intake/review/document-categories") {
      return { items: [] };
    }
    if (endpoint === "/api/intake/review/1") return PROPOSAL;
    if (/^\/api\/intake\/review\/clients\//.test(endpoint)) {
      return { items: [] };
    }
    if (/\/clients\/\d+\/practices$/.test(endpoint)) return { items: [] };
    throw new Error(`Unexpected GET ${endpoint}`);
  });
  apiMock.post.mockImplementation(async (endpoint: string) => {
    if (endpoint.endsWith("/claim")) {
      return {
        proposal_id: 1,
        claim_token: "tok-1",
        lease_expires_at: "2026-06-15T10:15:00Z",
      };
    }
    return {};
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReviewPage reject prompt", () => {
  it("rejects nothing when the reviewer cancels the prompt", async () => {
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);
    const reject = await openTheDocument();

    fireEvent.click(reject);

    await waitFor(() => expect(prompt).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(reject).toBeEnabled());
    expect(rejectCalls()).toHaveLength(0);
    // The document is still open, to approve or to reject on purpose.
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });

  it("rejects with the reason that was typed", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("wrong person");
    const reject = await openTheDocument();

    fireEvent.click(reject);

    await waitFor(() => expect(rejectCalls()).toHaveLength(1));
    expect(rejectCalls()[0]).toEqual([
      "/api/intake/review/1/reject",
      { claim_token: "tok-1", reason: "wrong person" },
    ]);
  });

  it("rejects with an empty reason when the prompt is confirmed empty", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("");
    const reject = await openTheDocument();

    fireEvent.click(reject);

    await waitFor(() => expect(rejectCalls()).toHaveLength(1));
    expect(rejectCalls()[0][1]).toEqual({ claim_token: "tok-1", reason: "" });
  });
});

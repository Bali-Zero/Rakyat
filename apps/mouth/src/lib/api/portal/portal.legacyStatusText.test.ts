import { describe, it, expect, vi, beforeEach } from "vitest";
import { PortalApi } from "./portal.api";
import type { ApiClientBase } from "../client";

// Status-update messages stored before PR 8486 carry the internal practice
// code ("visa_d12_business_1yr") in subject and content. The backend no longer
// writes them, and stored rows are not rewritten, so the portal reader names
// the service when it shows them. Fixtures are synthetic.

function apiReturning(messages: Record<string, unknown>[]) {
  const request = vi.fn().mockResolvedValue({
    data: { messages, total: messages.length, unread_count: 0 },
  });
  return new PortalApi({ request } as unknown as ApiClientBase);
}

const LEGACY = {
  id: 1,
  from_team: true,
  sent_by: "Bali Zero",
  subject: "Status update: visa_d12_business_1yr",
  content:
    "Your visa_d12_business_1yr status has been updated to Waiting Documents.",
  practice_id: 41,
  created_at: "2026-09-25T00:00:00+00:00",
  is_read: true,
};

describe("portal messages — legacy status-update text", () => {
  let first: (api: PortalApi) => Promise<{ subject?: string; content: string }>;
  beforeEach(() => {
    first = async (api) => (await api.getMessages()).messages[0];
  });

  it("names the service from practice_name when the API sends it", async () => {
    const msg = await first(
      apiReturning([{ ...LEGACY, practice_name: "D12 Business Visa, 1 year" }]),
    );
    expect(msg.subject).toBe("Status update: D12 Business Visa, 1 year");
    expect(msg.content).toBe(
      "Your D12 Business Visa, 1 year status has been updated to Waiting Documents.",
    );
  });

  it("without practice_name falls back to the code without underscores (backend rule)", async () => {
    const msg = await first(apiReturning([{ ...LEGACY, practice_name: null }]));
    expect(msg.subject).toBe("Status update: visa d12 business 1yr");
    expect(msg.content).not.toContain("_");
  });

  it("leaves a message already written with the human name untouched", async () => {
    const current = {
      ...LEGACY,
      subject: "Status update: D12 Business Visa, 1 year",
      content:
        "Your D12 Business Visa, 1 year status has been updated to Completed.",
      practice_name: "D12 Business Visa, 1 year",
    };
    const msg = await first(apiReturning([current]));
    expect(msg.subject).toBe(current.subject);
    expect(msg.content).toBe(current.content);
  });

  it("control — ordinary client text with underscores is not touched", async () => {
    const text =
      "My file is passport_scan_v2.pdf, status update: pending_review";
    const msg = await first(
      apiReturning([
        {
          id: 2,
          from_team: false,
          subject: null,
          content: text,
          practice_name: "D12 Business Visa, 1 year",
        },
      ]),
    );
    expect(msg.content).toBe(text);
    expect(msg.subject).toBeUndefined();
  });
});

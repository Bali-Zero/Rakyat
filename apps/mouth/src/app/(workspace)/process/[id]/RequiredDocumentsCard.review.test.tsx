/**
 * A required-document slot can be reviewed even when the client did not
 * upload through that slot.
 *
 * Case 21 Oct 2026 (process 1324, client_id 12666): the client sent the
 * passport through the portal Vault, so the "Passport" slot stayed
 * `pending` with `uploaded_by_client = false`. The Review button rendered
 * only for `uploaded_by_client`, so staff had no way to mark the slot and
 * an admin had to PATCH it from the browser console.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RequiredDocument } from "@/lib/types/required-documents";

const docs: RequiredDocument[] = [];

vi.mock("@/lib/hooks/useRequiredDocuments", () => ({
  useRequiredDocuments: () => ({
    documents: docs,
    isLoading: false,
    stats: {
      total: docs.length,
      verified: 0,
      uploaded: 0,
      pending: docs.length,
    },
    addDocument: vi.fn(),
    updateDocument: vi.fn(),
    deleteDocument: vi.fn(),
  }),
}));

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { dismiss: vi.fn() }),
}));

import { RequiredDocumentsCard } from "./RequiredDocumentsCard";

function slot(over: Partial<RequiredDocument>): RequiredDocument {
  return {
    id: 262,
    practice_id: 1324,
    document_type: "passport",
    document_label: "Passport",
    description: null,
    is_required: true,
    uploaded_by_client: false,
    uploaded_file_id: null,
    uploaded_at: null,
    client_notes: null,
    team_member_notes: null,
    status: "pending",
    created_at: "2026-10-21T06:16:25Z",
    updated_at: "2026-10-21T06:16:25Z",
    ...over,
  } as RequiredDocument;
}

describe("RequiredDocumentsCard review action", () => {
  it("offers a status action on a pending slot the client did not upload through", () => {
    docs.splice(0, docs.length, slot({}));
    render(<RequiredDocumentsCard practiceId={1324} />);
    const btn = screen.getByRole("button", { name: /update status/i });
    fireEvent.click(btn);
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText(/not uploaded through this slot/i)).toBeTruthy();
  });

  it("keeps the Review action for a slot the client uploaded (positive control)", () => {
    docs.splice(
      0,
      docs.length,
      slot({
        uploaded_by_client: true,
        status: "uploaded",
        uploaded_file_id: 20446,
      }),
    );
    render(<RequiredDocumentsCard practiceId={1324} />);
    fireEvent.click(screen.getByRole("button", { name: /review document/i }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.queryByText(/not uploaded through this slot/i)).toBeNull();
  });
});

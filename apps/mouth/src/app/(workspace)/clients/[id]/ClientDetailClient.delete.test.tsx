import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientProfile } from "@/lib/api/crm/crm.types";

/**
 * CRM-18: Delete client is a soft delete behind a two-step confirmation.
 * Before, one click on the toast's "Delete" action fired the request.
 * Synthetic data only.
 */

const { mockDeleteClient, mockUseClientDetail, mockRouterPush } = vi.hoisted(
  () => ({
    mockDeleteClient: vi.fn(),
    mockUseClientDetail: vi.fn(),
    mockRouterPush: vi.fn(),
  }),
);

const VIEWER = "synthetic.team@example.test";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "7" }),
  useRouter: () => ({ back: vi.fn(), push: mockRouterPush, replace: vi.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getProfile: vi.fn().mockResolvedValue({ email: VIEWER }),
    getUserProfile: vi.fn(() => ({ email: VIEWER })),
    crm: {
      updateClient: vi.fn(),
      createInteraction: vi.fn(),
      deleteClient: mockDeleteClient,
      getClientAiSummary: vi
        .fn()
        .mockResolvedValue({ status: "not_generated" }),
      getClientWaCaseIntelligence: vi
        .fn()
        .mockResolvedValue({ status: "not_generated", cases: [] }),
      getPortalStatus: vi.fn().mockResolvedValue({ has_portal_access: false }),
      getPortalMessages: vi.fn().mockResolvedValue({ messages: [] }),
    },
  },
}));

vi.mock("@/hooks/useClientDetail", () => ({
  useClientDetail: mockUseClientDetail,
  useClientTimeline: () => ({ data: [] }),
  useDocumentCategories: () => ({ data: [] }),
  useClientBusinessStory: () => ({ data: [], error: null, isLoading: false }),
  useInvalidateClient: () => vi.fn(),
  useInvalidateClientProfile: () => vi.fn(),
  useSetClientCache: () => vi.fn(),
}));
vi.mock("@/hooks/useTeamMembers", () => ({
  useTeamMemberOptions: () => ({ options: [] }),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn(), debug: vi.fn() },
}));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  }),
}));

// The trash button lives in OverviewTab; the stub keeps only that wire.
vi.mock("./components/OverviewTab", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./components/OverviewTab")>()),
  OverviewTab: (props: { onDeleteClick: () => void }) => (
    <button type="button" onClick={props.onDeleteClick}>
      Delete client
    </button>
  ),
}));
vi.mock("./components/BusinessStoryPanel", () => ({
  BusinessStoryPanel: () => null,
}));
vi.mock("./components/PortalMessages", () => ({ PortalMessages: () => null }));

const profile = (): ClientProfile => ({
  client: {
    id: 7,
    uuid: "synthetic-client-7",
    full_name: "Synthetic Delete Client",
    email: "synthetic.delete@example.test",
    status: "active",
    client_type: "individual",
    assigned_to: VIEWER,
    created_at: "2026-07-27T00:00:00Z",
    updated_at: "2026-07-27T00:00:00Z",
  },
  family_members: [],
  documents: [],
  expiry_alerts: [],
  practices: [],
  company_links: [],
  stats: {
    family_count: 0,
    documents_count: 0,
    practices_count: 0,
    expired_count: 0,
    red_alerts: 0,
    yellow_alerts: 0,
  },
});

describe("Delete client (CRM-18)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseClientDetail.mockReturnValue({
      data: profile(),
      isLoading: false,
      error: null,
    });
    mockDeleteClient.mockResolvedValue({ success: true, message: "ok" });
  });

  it("needs two steps and the typed name before the soft delete fires", async () => {
    const user = userEvent.setup();
    const { ClientDetailClient } = await import("./ClientDetailClient");
    render(<ClientDetailClient taxConsultants={[]} />);

    await user.click(
      await screen.findByRole("button", { name: "Delete client" }),
    );

    // Step 1: explains it is a soft delete.
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/marked inactive/i);
    expect(dialog).toHaveTextContent(/not erased/i);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // Step 2: final button stays disabled until the name matches.
    const final = screen.getByRole("button", { name: "Delete client" });
    expect(final).toBeDisabled();
    await user.type(
      screen.getByLabelText("Client name"),
      "Synthetic Delete Clien",
    );
    expect(final).toBeDisabled();
    await user.type(screen.getByLabelText("Client name"), "t");
    expect(final).toBeEnabled();
    expect(mockDeleteClient).not.toHaveBeenCalled();

    await user.click(final);
    await waitFor(() =>
      expect(mockDeleteClient).toHaveBeenCalledWith(7, VIEWER),
    );
    expect(mockRouterPush).toHaveBeenCalledWith("/clients");
  });

  it("innocence: Cancel never deletes", async () => {
    const user = userEvent.setup();
    const { ClientDetailClient } = await import("./ClientDetailClient");
    render(<ClientDetailClient taxConsultants={[]} />);

    await user.click(
      await screen.findByRole("button", { name: "Delete client" }),
    );
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(mockDeleteClient).not.toHaveBeenCalled();
  });
});

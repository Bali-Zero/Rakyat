import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewTab } from "./OverviewTab";
import { api } from "@/lib/api";
import type { ClientProfile, Interaction } from "@/lib/api/crm/crm.types";

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getClientAiSummary: vi
        .fn()
        .mockResolvedValue({ status: "not_generated" }),
      getClientWaCaseIntelligence: vi
        .fn()
        .mockResolvedValue({ status: "not_generated", cases: [] }),
      createInteraction: vi.fn().mockResolvedValue({}),
    },
    post: vi.fn(),
    request: vi.fn(),
  },
}));
vi.mock("./VaultCard", () => ({ VaultCard: () => <div /> }));

const daysAgo = (n: number) =>
  new Date(Date.now() - n * 86_400_000).toISOString();

const client = {
  id: 42,
  full_name: "Sample Client",
  email: "sample.client@example.test",
  last_interaction_date: daysAgo(45),
} as unknown as ClientProfile["client"];

const baseProps = {
  client,
  stats: {} as ClientProfile["stats"],
  documents: [],
  activePractices: [],
  completedPractices: [],
  attention: [],
  needsViewerAction: false,
  formatDate: (d: string) => d,
  formatCurrency: (n: number) => String(n),
  onEditClick: vi.fn(),
  onDeleteClick: vi.fn(),
  canDeleteClient: true,
  isDeletingClient: false,
  clientId: 42,
  viewerEmail: "staff@example.test",
};

describe("OverviewTab — Log contact on the Last-contact fact", () => {
  beforeEach(() => vi.clearAllMocks());

  it("after a logged contact the page refresh brings Last contact back to Today and clears the silent tone", async () => {
    const fresh = {
      id: 1,
      interaction_type: "call",
      summary: "Call",
      interaction_date: new Date().toISOString(),
    } as unknown as Interaction;
    let rerenderTab: (i: Interaction[]) => void = () => {};
    const onRefresh = vi.fn(async () => rerenderTab([fresh]));
    const { rerender } = render(
      <OverviewTab {...baseProps} interactions={[]} onRefresh={onRefresh} />,
    );
    rerenderTab = (i) =>
      rerender(
        <OverviewTab {...baseProps} interactions={i} onRefresh={onRefresh} />,
      );
    const fact = () => screen.getByTestId("client-last-contact");
    expect(fact().textContent).toContain("silent for 45 days");

    fireEvent.click(screen.getByTestId("log-contact-trigger"));
    fireEvent.click(screen.getByRole("radio", { name: "Call" }));
    fireEvent.click(screen.getByTestId("log-contact-submit"));

    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
    expect(api.crm.createInteraction).toHaveBeenCalledWith(
      expect.objectContaining({ client_id: 42, interaction_type: "call" }),
    );
    await waitFor(() => expect(fact().textContent).toContain("Today"));
    expect(fact().textContent).not.toContain("silent for");
  });
});

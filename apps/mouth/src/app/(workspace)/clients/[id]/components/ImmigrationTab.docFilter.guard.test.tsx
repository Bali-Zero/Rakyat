import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { useFixedClock } from "./fixedClock.testutil";
import { ImmigrationTab } from "./ImmigrationTab";

vi.mock("./AiSummaryCard", () => ({ AiSummaryCard: () => null }));
vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      deleteDocument: vi.fn(),
      getClientRecap: vi.fn().mockRejectedValue(new Error("no recap")),
    },
  },
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

beforeEach(() => useFixedClock());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/**
 * The Immigration tab used to take any document whose type merely CONTAINED
 * "permit" or "visa": a building permit or an Indonesian "devisa" (foreign
 * exchange) record landed among the client's stay permits — and, through the
 * same predicate in ClientDetailClient, vanished from the Documents tab.
 */
const doc = (id: number, document_type: string): ClientDocument => ({
  id,
  client_id: 1,
  document_type,
});

const renderTab = (documents: ClientDocument[]) =>
  render(
    <ImmigrationTab
      clientId={1}
      documents={documents}
      formatDate={(d) => d}
      onAddClick={vi.fn()}
      onEditClick={vi.fn()}
      onRefresh={vi.fn()}
    />,
  );

const NON_IMMIGRATION = [
  "building_permit",
  "Business Permit",
  "environmental permit",
  "devisa transfer receipt",
];

const shown = (documents: ClientDocument[]) =>
  (renderTab(documents).container.textContent ?? "").toLowerCase();

describe("ImmigrationTab — document filter", () => {
  it("still lists real immigration documents (positive control)", () => {
    const text = shown([doc(1, "stay_permit"), doc(2, "work permit")]);
    expect(text).toContain("stay permit");
    expect(text).toContain("work permit");
  });

  it.each(NON_IMMIGRATION)(
    "a %s document is not an immigration document",
    (type) => {
      const text = shown([doc(1, "stay_permit"), doc(9, type)]);
      expect(text).toContain("stay permit");
      expect(text).not.toContain(type.replace(/_/g, " ").toLowerCase());
    },
  );
});

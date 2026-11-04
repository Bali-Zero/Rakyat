import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ClientProfile } from "@/lib/api/crm/crm.types";
import { ProcessTab } from "./ProcessTab";

vi.mock("@/lib/api", () => ({ api: { crm: { deletePractice: vi.fn() } } }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { dismiss: vi.fn() }),
}));
vi.mock("./AiSummaryCard", () => ({ AiSummaryCard: () => null }));

const inDays = (n: number) =>
  new Date(Date.now() + n * 86_400_000).toISOString().slice(0, 10);

type P = ClientProfile["practices"][number];
const oldVisa = {
  id: 41,
  status: "completed",
  practice_type_code: "visa_b211a_tourist",
  practice_type_name: "Tourist Visa",
  expiry_date: inDays(121),
  alert_color: "red",
} as P;
const newKitas = {
  id: 311,
  status: "completed",
  practice_type_code: "kitas_investor_2yr_onshore",
  practice_type_name: "KITAS Investor",
} as P;

function chipFor(expiryText: RegExp) {
  return screen.getByText(expiryText).closest("div") as HTMLElement;
}

describe("ProcessTab — expiry chip of a replaced visa (client 10247 shape)", () => {
  it("GUILT: the replaced visa's chip is neutral and says it was replaced", () => {
    render(
      <ProcessTab
        clientId={1}
        practices={[oldVisa, newKitas]}
        formatDate={(d) => d}
        onRefresh={vi.fn()}
      />,
    );
    const chip = chipFor(/replaced/);
    expect(chip.className).not.toContain("--state-warning");
  });

  it("control: without the later KITAS the same chip keeps its warning tone", () => {
    render(
      <ProcessTab
        clientId={1}
        practices={[oldVisa]}
        formatDate={(d) => d}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.queryByText(/replaced/)).toBeNull();
    expect(chipFor(/121d left/).className).toContain("--state-warning");
  });
});

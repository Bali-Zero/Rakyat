import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ error: vi.fn(), success: vi.fn() }),
}));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn(), info: vi.fn() } }));
vi.mock("@/lib/api/workspace/lkpm.api", () => ({
  lkpmApi: {
    getBatch: vi.fn().mockResolvedValue({
      items: [
        { id: 1, company_name: "Co A", status: "draft", lkpm_assigned_to: "retired.tax@example.test" },
        { id: 2, company_name: "Co B", status: "draft", lkpm_assigned_to: "kadek.tax@example.test" },
        { id: 3, company_name: "Co C", status: "draft", lkpm_assigned_to: null },
      ],
    }),
    getDeadlines: vi.fn().mockResolvedValue([]),
    assignReport: vi.fn(),
  },
}));

const OPTIONS = [{ value: "kadek.tax@example.test", label: "Kadek" }];

describe("LKPM assignee select keeps a stored value that is no longer offered", () => {
  it("shows a preserved 'Former staff' option for it, and not for offered or empty values", async () => {
    const { default: LkpmBatchClient } = await import("./LkpmBatchClient");
    render(<LkpmBatchClient taxConsultants={OPTIONS} />);
    const gone = (await waitFor(() =>
      screen.getByLabelText("Assign Co A tax consultant"),
    )) as HTMLSelectElement;
    expect(gone.value).toBe("retired.tax@example.test");
    expect(gone.options[gone.selectedIndex].textContent).toBe("Former staff");
    const offered = screen.getByLabelText("Assign Co B tax consultant") as HTMLSelectElement;
    expect(offered.value).toBe("kadek.tax@example.test");
    expect(Array.from(offered.options).map((o) => o.textContent)).not.toContain("Former staff");
    const empty = screen.getByLabelText("Assign Co C tax consultant") as HTMLSelectElement;
    expect(empty.value).toBe("");
    expect(Array.from(empty.options).map((o) => o.textContent)).not.toContain("Former staff");
  });
});

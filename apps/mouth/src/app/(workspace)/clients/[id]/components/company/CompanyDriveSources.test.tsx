import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CompanyDriveSources } from "./CompanyDriveSources";
import type { CompanyDriveSources as Sources } from "@/lib/api/crm/crm.types";

const { getSources, mutation } = vi.hoisted(() => ({
  getSources: vi.fn(),
  mutation: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: { crm: { getCompanyDriveSources: getSources }, post: mutation },
}));

const EMPTY: Sources = {
  company_id: 17,
  sources: [],
  lineage_status: "none",
  merged_into_company_id: null,
  historical_record_ids: [],
};
const URL =
  "https://drive.google.com/drive/folders/1SyntheticDriveFolderAbCdEf123456";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("informational company Drive sources", () => {
  it("renders deduplicated direct links with provenance and history, without mutation calls", async () => {
    getSources.mockResolvedValue({
      ...EMPTY,
      sources: [{ url: URL, kinds: ["primary", "restricted_tax", "legacy"] }],
      historical_record_ids: [8],
    });
    render(<CompanyDriveSources companyId={17} />);
    const link = await screen.findByRole("link", {
      name: "Primary · Restricted tax archive · Legacy archive",
    });
    expect(link).toHaveAttribute("href", URL);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.getByText("Historical record #8")).toBeVisible();
    expect(screen.getByText(/Existing Drive permissions apply/)).toBeVisible();
    expect(getSources).toHaveBeenCalledWith(17);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(mutation).not.toHaveBeenCalled();
  });

  it("shows the empty state", async () => {
    getSources.mockResolvedValue(EMPTY);
    render(<CompanyDriveSources companyId={17} />);
    expect(await screen.findByText("No Drive sources recorded.")).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows an error without exposing the raw response", async () => {
    getSources.mockRejectedValue(new Error("sensitive response"));
    render(<CompanyDriveSources companyId={17} />);
    expect(
      await screen.findByText("Drive sources are unavailable."),
    ).toBeVisible();
    expect(screen.queryByText(/sensitive response/)).not.toBeInTheDocument();
  });

  it("only displays validated lineage as text", async () => {
    getSources.mockResolvedValue({
      ...EMPTY,
      lineage_status: "valid",
      merged_into_company_id: 18,
    });
    render(<CompanyDriveSources companyId={17} />);
    expect(
      await screen.findByText("Historical record #17 · merged into record #18"),
    ).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows an invalid reference without an identifier", async () => {
    getSources.mockResolvedValue({ ...EMPTY, lineage_status: "invalid" });
    render(<CompanyDriveSources companyId={17} />);
    expect(
      await screen.findByText("Historical reference unavailable."),
    ).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("does not retain links or accept late responses from the previous company", async () => {
    let resolveOld!: (value: Sources) => void;
    getSources.mockImplementationOnce(
      () =>
        new Promise<Sources>((resolve) => {
          resolveOld = resolve;
        }),
    );
    const { rerender } = render(<CompanyDriveSources companyId={17} />);
    expect(screen.getByText("Loading Drive sources…")).toBeVisible();
    getSources.mockResolvedValue({ ...EMPTY, company_id: 18 });
    rerender(<CompanyDriveSources companyId={18} />);
    await screen.findByText("No Drive sources recorded.");
    await act(async () =>
      resolveOld({ ...EMPTY, sources: [{ url: URL, kinds: ["primary"] }] }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("link")).not.toBeInTheDocument(),
    );
  });
});

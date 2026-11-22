/**
 * "Show" on a batch row asks for the OSS login of THAT row's company. One client can
 * hold a row per company; asking by client alone could show another company's login.
 * Invented values only.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const getCredentials = vi.hoisted(() => vi.fn());

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ error: vi.fn(), success: vi.fn() }),
}));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn(), info: vi.fn() } }));
vi.mock("@/lib/api/workspace/lkpm.api", () => ({
  lkpmApi: {
    getBatch: vi.fn().mockResolvedValue({
      items: [
        {
          id: 1,
          client_id: 900007,
          company_id: 11,
          company_name: "PT Contoh Satu",
          status: "draft",
          lkpm_assigned_to: null,
        },
        {
          id: 2,
          client_id: 900007,
          company_id: 22,
          company_name: "PT Contoh Dua",
          status: "draft",
          lkpm_assigned_to: null,
        },
        {
          id: 3,
          client_id: 900008,
          company_id: null,
          company_name: "PT Contoh Tiga",
          status: "draft",
          lkpm_assigned_to: null,
        },
      ],
    }),
    getDeadlines: vi.fn().mockResolvedValue([]),
    assignReport: vi.fn(),
    getCredentials,
  },
}));

async function showCredentialsOfRow(index: number) {
  const { default: LkpmBatchClient } = await import("./LkpmBatchClient");
  render(<LkpmBatchClient taxConsultants={[]} />);
  const buttons = await waitFor(() => {
    const found = screen.getAllByLabelText("Show OSS credentials");
    expect(found).toHaveLength(3);
    return found;
  });
  fireEvent.click(buttons[index]);
  await waitFor(() => expect(getCredentials).toHaveBeenCalledTimes(1));
}

describe("LKPM batch: OSS credentials of a row", () => {
  it("asks for the company of the row that was clicked", async () => {
    getCredentials.mockReset().mockResolvedValue({
      client_id: 900007,
      company_name: "PT Contoh Dua",
      oss_username: "login-stub",
      oss_password: "word-stub", // pragma: allowlist secret
      oss_creds_updated_at: null,
      oss_creds_updated_by: null,
    });

    await showCredentialsOfRow(1);

    expect(getCredentials).toHaveBeenCalledWith(900007, 22);
  });

  it("asks by client alone for a row that has no company", async () => {
    getCredentials.mockReset().mockResolvedValue({
      client_id: 900008,
      company_name: "PT Contoh Tiga",
      oss_username: "login-stub",
      oss_password: "word-stub", // pragma: allowlist secret
      oss_creds_updated_at: null,
      oss_creds_updated_by: null,
    });

    await showCredentialsOfRow(2);

    expect(getCredentials).toHaveBeenCalledWith(900008, null);
  });
});

/**
 * OSS credentials are asked for ONE company: a client can report for several, each
 * with its own login, so the company of the row travels with the request.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));

import { api } from "@/lib/api";
import { lkpmApi } from "./lkpm.api";

const getMock = vi.mocked(api.get);

beforeEach(() => {
  vi.clearAllMocks();
  getMock.mockResolvedValue({
    success: true,
    client_id: 900007,
    company_name: "PT Contoh Dua",
    oss_username: "login-stub",
    oss_password: "word-stub", // pragma: allowlist secret
    oss_creds_updated_at: null,
    oss_creds_updated_by: null,
  });
});

describe("lkpmApi.getCredentials", () => {
  it("names the company of the row", async () => {
    await lkpmApi.getCredentials(900007, 22);

    expect(getMock).toHaveBeenCalledWith(
      "/api/v1/lkpm/credentials/900007?company_id=22",
    );
  });

  it.each([undefined, null])(
    "asks by client alone when the row has no company (%s)",
    async (companyId) => {
      await lkpmApi.getCredentials(900007, companyId);

      expect(getMock).toHaveBeenCalledWith("/api/v1/lkpm/credentials/900007");
    },
  );

  it("returns what the server answered for that company", async () => {
    const creds = await lkpmApi.getCredentials(900007, 22);

    expect(creds.company_name).toBe("PT Contoh Dua");
    expect(creds.oss_username).toBe("login-stub");
  });
});

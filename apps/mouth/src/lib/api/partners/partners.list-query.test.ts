import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));

import { listActivePartnersDropdown, listPartners } from "./partners";

/** The query of the one request made, as the server would read it. */
function sent(): { path: string; query: Record<string, string> } {
  expect(api.get).toHaveBeenCalledTimes(1);
  const url = new URL(api.get.mock.calls[0][0] as string, "http://kita.test");
  return { path: url.pathname, query: Object.fromEntries(url.searchParams) };
}

// The server's list route declares `onboarding_status` and has no `status`
// parameter (apps/backend-rag, app/routers/partners.py, list_partners); the
// backend test test_the_partner_list_filters_on_the_key_the_workspace_sends.py
// shows on the real table what each of the two keys does.
describe("the partner list request", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.get.mockResolvedValue({ partners: [], total: 0 });
  });

  it("sends the status filter under the key the server reads", async () => {
    await listPartners({ status: "pending_approval", page: 2, page_size: 50 });

    expect(sent()).toEqual({
      path: "/api/partners",
      query: {
        onboarding_status: "pending_approval",
        page: "2",
        page_size: "50",
      },
    });
  });

  it("keeps the other filters as they were", async () => {
    await listPartners({
      status: "inactive",
      assigned_to: "team-1",
      orphaned: true,
      search: "zz",
    });

    expect(sent().query).toEqual({
      onboarding_status: "inactive",
      assigned_to: "team-1",
      orphaned: "true",
      search: "zz",
    });
  });

  it.each([undefined, ""])("sends no status key for %j", async (status) => {
    await listPartners({ status, search: "zz" });

    expect(sent().query).toEqual({ search: "zz" });
  });

  it("asks for the bare list when there is no filter", async () => {
    await listPartners();

    expect(api.get).toHaveBeenCalledWith("/api/partners");
  });

  it("asks for active partners only in the referrer picker", async () => {
    await listActivePartnersDropdown();

    expect(sent()).toEqual({
      path: "/api/partners",
      query: { onboarding_status: "active", page_size: "200" },
    });
  });
});

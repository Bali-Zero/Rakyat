import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import {
  isNonHumanRole,
  isTeamRole,
  readStoredTeamMembers,
  TEAM_MEMBERS_STALE_MS,
  useTeamMemberOptions,
  useTeamMembers,
} from "./useTeamMembers";

vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(),
    getUserProfile: vi.fn(() => null),
  },
}));

const createClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

const wrapperFor =
  (queryClient: QueryClient) =>
  ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );

describe("useTeamMemberOptions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("deduplicates normalized emails and disambiguates repeated names", async () => {
    vi.mocked(api.get).mockResolvedValue([
      {
        id: "member-1",
        email: " ari@example.test ",
        full_name: "Ari Example",
        name: "Ari",
        role: "team",
        avatar_url: null,
        avatar: null,
      },
      {
        id: "member-1-copy",
        email: "ARI@EXAMPLE.TEST",
        full_name: "Ari Example",
        name: "Ari duplicate",
        role: "team",
        avatar_url: null,
        avatar: null,
      },
      {
        id: "member-2",
        email: "other@example.test",
        full_name: "Ari Example",
        name: "Other Ari",
        role: "tax",
        avatar_url: null,
        avatar: null,
      },
      {
        id: "client-1",
        email: "client@example.test",
        full_name: "Client Example",
        name: "Client",
        role: "client",
        avatar_url: null,
        avatar: null,
      },
    ]);

    const queryClient = createClient();
    const { result } = renderHook(() => useTeamMemberOptions(), {
      wrapper: wrapperFor(queryClient),
    });

    await waitFor(() => {
      expect(result.current.options).toHaveLength(2);
    });
    expect(result.current.options).toEqual([
      {
        value: "ari@example.test",
        label: "Ari Example (ari@example.test)",
        avatar: undefined,
      },
      {
        value: "other@example.test",
        label: "Ari Example (other@example.test)",
        avatar: undefined,
      },
    ]);
  });

  it("excludes service accounts (role monitoring) alongside clients", async () => {
    vi.mocked(api.get).mockResolvedValue([
      {
        id: "human-1",
        email: "ari@example.test",
        full_name: "Ari Example",
        name: "Ari",
        role: "Tax Care",
        avatar_url: null,
        avatar: null,
      },
      {
        id: "probe-1",
        email: "probe@balizero.com",
        full_name: "Login Healthcheck Probe",
        name: "Probe",
        role: "monitoring",
        avatar_url: null,
        avatar: null,
      },
    ]);

    const queryClient = createClient();
    const { result } = renderHook(() => useTeamMemberOptions(), {
      wrapper: wrapperFor(queryClient),
    });

    await waitFor(() => {
      expect(result.current.options).toHaveLength(1);
    });
    expect(result.current.options[0].value).toBe("ari@example.test");
  });
});

describe("isNonHumanRole", () => {
  it("flags clients and service accounts (guilt)", () => {
    expect(isNonHumanRole("client")).toBe(true);
    expect(isNonHumanRole("Client")).toBe(true);
    expect(isNonHumanRole("monitoring")).toBe(true);
    expect(isNonHumanRole("  MONITORING  ")).toBe(true);
  });

  it("leaves real, free-text team roles alone (innocence)", () => {
    for (const role of [
      "Tax Care",
      "Reception",
      "Board Member",
      "admin",
      "Founder",
    ]) {
      expect(isNonHumanRole(role)).toBe(false);
    }
  });
});

describe("isTeamRole", () => {
  it("admits every role the backend allow-list admits (innocence)", () => {
    expect(isTeamRole("Tax Lead")).toBe(true);
    expect(isTeamRole("Tax Consultant")).toBe(true);
    expect(isTeamRole("  board member ")).toBe(true);
    expect(isTeamRole("member")).toBe(true);
  });

  it("refuses partners, clients, service accounts and the unknown (guilt)", () => {
    expect(isTeamRole("partner")).toBe(false);
    expect(isTeamRole("client")).toBe(false);
    expect(isTeamRole("monitoring")).toBe(false);
    expect(isTeamRole("user")).toBe(false);
    expect(isTeamRole("")).toBe(false);
    expect(isTeamRole(null)).toBe(false);
    expect(isTeamRole(undefined)).toBe(false);
  });
});

describe("useTeamMembers cache across page loads", () => {
  const member = {
    id: "m-1",
    email: "ari@example.test",
    full_name: "Ari Example",
    name: "Ari",
    role: "team",
    avatar_url: null,
    avatar: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    vi.mocked(api.getUserProfile).mockReturnValue({
      email: "Viewer@Example.test",
    } as never);
  });

  it("a second full load in the same tab reuses the roster without a request", async () => {
    vi.mocked(api.get).mockResolvedValue([member]);
    const first = renderHook(() => useTeamMembers(), {
      wrapper: wrapperFor(createClient()),
    });
    await waitFor(() => expect(first.result.current.data).toEqual([member]));
    expect(api.get).toHaveBeenCalledTimes(1);

    // A fresh QueryClient = a full page load (memory cache gone).
    const second = renderHook(() => useTeamMembers(), {
      wrapper: wrapperFor(createClient()),
    });
    expect(second.result.current.data).toEqual([member]);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("never hands one viewer's stored roster to another login", () => {
    window.sessionStorage.setItem(
      "kita:team-members:v1",
      JSON.stringify({
        viewer: "someone@else.test",
        at: Date.now(),
        members: [member],
      }),
    );
    expect(readStoredTeamMembers()).toBeUndefined();
  });

  it("ignores a stored roster older than the stale window", () => {
    const at = Date.now();
    window.sessionStorage.setItem(
      "kita:team-members:v1",
      JSON.stringify({ viewer: "viewer@example.test", at, members: [member] }),
    );
    expect(readStoredTeamMembers(at + 1)?.members).toEqual([member]);
    expect(readStoredTeamMembers(at + TEAM_MEMBERS_STALE_MS)).toBeUndefined();
  });
});

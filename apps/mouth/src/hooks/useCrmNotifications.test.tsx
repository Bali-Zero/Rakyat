import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useCrmNotifications } from "./useCrmNotifications";

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      request: vi.fn(),
      getExpiryAlerts: vi.fn(),
    },
  },
}));
vi.mock("@/lib/logger");

const expiry = (
  entityId: number,
  documentType = "passport",
  entityType = "client",
) => ({
  client_id: 900000 + entityId,
  entity_type: entityType,
  entity_id: entityId,
  entity_name: `Person ${entityId}`,
  document_type: documentType,
  expiry_date: "2027-01-01",
  days_until_expiry: 30,
  alert_color: "yellow",
});

const portal = (id: number, status = "pending") => ({
  id,
  client_id: 900100 + id,
  alert_type: "portal_upload",
  status,
  message: "Uploaded",
  email_subject: null,
  created_at: "2026-06-01T10:00:00.000Z",
  sent_at: null,
});

const mockedExpiry = vi.mocked(api.crm.getExpiryAlerts);
const mockedRequest = vi.mocked(api.crm.request);

const setup = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useCrmNotifications({ autoRefresh: false }), {
    wrapper,
  });
  const poll = async () => {
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await queryClient.refetchQueries({ queryKey: ["crm", "notifications"] });
    });
    await act(async () => {
      await Promise.resolve();
    });
  };
  return { hook, poll };
};

describe("useCrmNotifications read state across polls", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-15T12:00:00.000Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows everything unread on the first load", async () => {
    mockedExpiry.mockResolvedValue([expiry(1), expiry(2)] as never);
    mockedRequest.mockResolvedValue([portal(1)] as never);
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.unreadCount).toBe(3));
  });

  it("keeps mark-all-read across the next poll", async () => {
    mockedExpiry.mockResolvedValue([expiry(1), expiry(2)] as never);
    mockedRequest.mockResolvedValue([portal(1)] as never);
    const { hook, poll } = setup();
    await waitFor(() => expect(hook.result.current.unreadCount).toBe(3));

    act(() => hook.result.current.markAllAsRead());
    expect(hook.result.current.unreadCount).toBe(0);

    await poll();
    expect(hook.result.current.notifications).toHaveLength(3);
    expect(hook.result.current.unreadCount).toBe(0);
    expect(hook.result.current.notifications.every((n) => n.read)).toBe(true);
  });

  it("keeps an expiry alert marked read after a poll", async () => {
    mockedExpiry.mockResolvedValue([expiry(1), expiry(2)] as never);
    mockedRequest.mockResolvedValue([] as never);
    const { hook, poll } = setup();
    await waitFor(() => expect(hook.result.current.unreadCount).toBe(2));

    act(() => hook.result.current.markAsRead("expiry-client-1-passport"));
    expect(hook.result.current.unreadCount).toBe(1);

    await poll();
    expect(hook.result.current.unreadCount).toBe(1);
    const first = hook.result.current.notifications.find(
      (n) => n.id === "expiry-client-1-passport",
    );
    expect(first?.read).toBe(true);
  });

  it("keeps two entity types that share an id and a document apart", async () => {
    mockedExpiry.mockResolvedValue([
      expiry(1, "passport", "client"),
      expiry(1, "passport", "family_member"),
    ] as never);
    mockedRequest.mockResolvedValue([] as never);
    const { hook, poll } = setup();
    await waitFor(() => expect(hook.result.current.unreadCount).toBe(2));
    const ids = hook.result.current.notifications.map((n) => n.id);
    expect(new Set(ids).size).toBe(2);

    act(() => hook.result.current.markAsRead("expiry-client-1-passport"));
    expect(hook.result.current.unreadCount).toBe(1);

    await poll();
    expect(hook.result.current.unreadCount).toBe(1);
    const readById = new Map(
      hook.result.current.notifications.map((n) => [n.id, n.read]),
    );
    expect(readById.get("expiry-client-1-passport")).toBe(true);
    expect(readById.get("expiry-family_member-1-passport")).toBe(false);
  });

  it("shows exactly one unread when a poll brings one new item", async () => {
    mockedExpiry.mockResolvedValue([expiry(1)] as never);
    mockedRequest.mockResolvedValue([portal(1)] as never);
    const { hook, poll } = setup();
    await waitFor(() => expect(hook.result.current.unreadCount).toBe(2));
    act(() => hook.result.current.markAllAsRead());
    expect(hook.result.current.unreadCount).toBe(0);

    mockedExpiry.mockResolvedValue([expiry(1), expiry(3)] as never);
    await poll();
    await waitFor(() =>
      expect(hook.result.current.notifications).toHaveLength(3),
    );
    expect(hook.result.current.unreadCount).toBe(1);
  });

  it("does not count the same item twice when marked read twice", async () => {
    mockedExpiry.mockResolvedValue([expiry(1), expiry(2)] as never);
    mockedRequest.mockResolvedValue([] as never);
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.unreadCount).toBe(2));
    act(() => {
      hook.result.current.markAsRead("expiry-client-1-passport");
      hook.result.current.markAsRead("expiry-client-1-passport");
    });
    expect(hook.result.current.unreadCount).toBe(1);
  });
});

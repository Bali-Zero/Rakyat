import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import TeamActivityPage from "./page";

const apiMock = vi.hoisted(() => ({
  isAdmin: vi.fn(),
  adminApi: {
    getTeamActivityOverview: vi.fn(),
    getTeamStats: vi.fn(),
    getTeamActivityMessages: vi.fn(),
    getTeamTimesheet: vi.fn(),
    getCrmActions: vi.fn(),
    exportMessages: vi.fn(),
  },
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/hooks/useSessionState", () => ({
  useSessionState: () => "authenticated",
}));
const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("next/image", () => ({
  default: (props: React.ImgHTMLAttributes<HTMLImageElement>) => (
    <img {...props} />
  ),
}));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));

const messages = (label = "latest", total = 150) => ({
  messages: [
    {
      conversation_id: 1,
      user_id: "synthetic@example.test",
      role: "assistant",
      content: label,
      message_timestamp: null,
      conversation_started: "2026-01-01",
    },
  ],
  total,
});
const timesheet = (total = 150) => ({ records: [], total });
const crm = (total = 150) => ({ actions: [], total });

beforeEach(() => {
  apiMock.isAdmin.mockReturnValue(true);
  apiMock.adminApi.getTeamActivityOverview.mockResolvedValue({
    stats: {
      total_conversations: 0,
      total_messages: 0,
      total_team_members: 0,
      active_today: 0,
      messages_today: 0,
    },
    top_users: [],
  });
  apiMock.adminApi.getTeamStats.mockResolvedValue({ team_stats: [] });
  apiMock.adminApi.getTeamActivityMessages.mockResolvedValue(messages());
  apiMock.adminApi.getTeamTimesheet.mockResolvedValue(timesheet());
  apiMock.adminApi.getCrmActions.mockResolvedValue(crm());
  apiMock.adminApi.exportMessages.mockResolvedValue(new Blob());
});

const last = <T,>(items: T[]): T => items[items.length - 1];
const latestMessageRequest = () =>
  last(apiMock.adminApi.getTeamActivityMessages.mock.calls)[0];

const renderPage = async () => {
  render(<TeamActivityPage />);
  await screen.findByRole("button", { name: "Messages" });
};

describe("team activity page", () => {
  it("exports the on-screen message role and search", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    await waitFor(() =>
      expect(apiMock.adminApi.getTeamActivityMessages).toHaveBeenCalled(),
    );
    fireEvent.change(screen.getByLabelText("Search messages"), {
      target: { value: "synthetic phrase" },
    });
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "assistant" },
    });
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    expect(apiMock.adminApi.exportMessages).toHaveBeenCalledWith({
      user_id: undefined,
      role: "assistant",
      search: "synthetic phrase",
    });
  });

  it("resets each filtered list to the first page", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    await waitFor(() =>
      expect(apiMock.adminApi.getTeamActivityMessages).toHaveBeenCalled(),
    );
    fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
    fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
    fireEvent.change(screen.getByLabelText("Search messages"), {
      target: { value: "narrow" },
    });
    await waitFor(() => expect(latestMessageRequest().offset).toBe(0));
    expect(screen.queryByText(/Showing 101/)).toBeNull();
    for (const [control, value] of [
      [screen.getByLabelText("Filter by user"), "synthetic@example.test"],
      [screen.getByRole("combobox"), "assistant"],
    ] as const) {
      fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
      fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
      await waitFor(() => expect(latestMessageRequest().offset).toBe(100));
      fireEvent.change(control, { target: { value } });
      await waitFor(() => expect(latestMessageRequest().offset).toBe(0));
    }

    fireEvent.click(screen.getByRole("button", { name: "Timesheet" }));
    await waitFor(() =>
      expect(apiMock.adminApi.getTeamTimesheet).toHaveBeenCalled(),
    );
    fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
    fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
    fireEvent.change(screen.getByLabelText("Filter by email"), {
      target: { value: "synthetic@example.test" },
    });
    await waitFor(() =>
      expect(last(apiMock.adminApi.getTeamTimesheet.mock.calls)[0].offset).toBe(
        0,
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "CRM Actions" }));
    await waitFor(() =>
      expect(apiMock.adminApi.getCrmActions).toHaveBeenCalled(),
    );
    fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
    fireEvent.click(last(screen.getAllByRole("button", { name: "" })));
    fireEvent.change(screen.getByLabelText("Filter CRM actions by email"), {
      target: { value: "synthetic@example.test" },
    });
    await waitFor(() =>
      expect(last(apiMock.adminApi.getCrmActions.mock.calls)[0].offset).toBe(0),
    );
  });

  it("keeps the latest message response when requests resolve in reverse order", async () => {
    let resolveFirst!: (value: ReturnType<typeof messages>) => void;
    let resolveSecond!: (value: ReturnType<typeof messages>) => void;
    apiMock.adminApi.getTeamActivityMessages
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve;
          }),
      );
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    await waitFor(() =>
      expect(apiMock.adminApi.getTeamActivityMessages).toHaveBeenCalledTimes(1),
    );
    fireEvent.change(screen.getByLabelText("Search messages"), {
      target: { value: "latest" },
    });
    await waitFor(() =>
      expect(apiMock.adminApi.getTeamActivityMessages).toHaveBeenCalledTimes(2),
    );
    await act(async () => {
      resolveSecond(messages("latest"));
    });
    await act(async () => {
      resolveFirst(messages("stale"));
    });
    expect(screen.getByText("latest")).toBeTruthy();
    expect(screen.queryByText("stale")).toBeNull();
  });
  it.each([
    {
      tab: "Timesheet",
      filter: "Filter by email",
      mock: apiMock.adminApi.getTeamTimesheet,
      answer: (label: string) => ({
        records: [
          {
            id: 1,
            email: "synthetic@example.test",
            action_type: "clock_in",
            timestamp: "2026-01-05T01:00:00Z",
            notes: label,
          },
        ],
        total: 1,
      }),
    },
    {
      tab: "CRM Actions",
      filter: "Filter CRM actions by email",
      mock: apiMock.adminApi.getCrmActions,
      answer: (label: string) => ({
        actions: [
          {
            id: 1,
            performed_by: "synthetic@example.test",
            action: "update",
            entity_type: "client",
            entity_id: 1,
            description: label,
            performed_at: "2026-01-05T01:00:00Z",
          },
        ],
        total: 1,
      }),
    },
  ])(
    "keeps the latest $tab response when requests resolve in reverse order",
    async ({ tab, filter, mock, answer }) => {
      let resolveFirst!: (value: unknown) => void;
      let resolveSecond!: (value: unknown) => void;
      mock
        .mockImplementationOnce(
          () => new Promise((resolve) => (resolveFirst = resolve)),
        )
        .mockImplementationOnce(
          () => new Promise((resolve) => (resolveSecond = resolve)),
        );
      await renderPage();
      fireEvent.click(screen.getByRole("button", { name: tab }));
      await waitFor(() => expect(mock).toHaveBeenCalledTimes(1));
      fireEvent.change(await screen.findByLabelText(filter), {
        target: { value: "synthetic" },
      });
      await waitFor(() => expect(mock).toHaveBeenCalledTimes(2));
      await act(async () => resolveSecond(answer("latest")));
      await act(async () => resolveFirst(answer("stale")));
      expect(screen.getByText("latest")).toBeTruthy();
      expect(screen.queryByText("stale")).toBeNull();
    },
  );

  it("shows no range when a filtered list is empty", async () => {
    apiMock.adminApi.getTeamActivityMessages.mockResolvedValue(
      messages("none", 0),
    );
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    await screen.findByText("none");
    expect(screen.queryByText(/Showing/)).toBeNull();
  });
});

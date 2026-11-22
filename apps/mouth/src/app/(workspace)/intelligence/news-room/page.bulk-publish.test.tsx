/**
 * "Publish all" in the News Room sends articles toward the public site. It acted on
 * every id ever selected: cards a search had hidden, and cards already queued. It
 * then said "completed" whatever happened, and emptied the selection, so a run where
 * every request failed looked like a success with nothing left to send again.
 *
 * It sends the selected cards that are on screen and still waiting, says a failure
 * is a failure, and keeps what failed selected. Invented articles only.
 */
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import NewsRoomPage from "./page";
import { intelligenceApi, type StagingItem } from "@/lib/api/intelligence.api";

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api/intelligence.api");
vi.mock("@/lib/logger");
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
// The position picker as a native select, so that a test can choose a position.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (value: string) => void;
    children?: ReactNode;
  }) => (
    <select
      value={value ?? ""}
      onChange={(event) => onValueChange?.(event.target.value)}
    >
      <option value="">none</option>
      {children}
    </select>
  ),
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectItem: ({
    value,
    children,
  }: {
    value: string;
    children?: ReactNode;
  }) => <option value={value}>{children}</option>,
}));

const ALPHA = "Alpha visa update";
const BRAVO = "Bravo tax notice";
const CHARLIE = "Charlie permit change";

function article(id: string, title: string, detected_at: string): StagingItem {
  return {
    id,
    type: "news",
    title,
    status: "pending",
    detected_at,
    source: "Example Source",
    detection_type: "NEW",
    is_critical: false,
  };
}

// Newest first on screen: Alpha, Bravo, Charlie.
const ARTICLES = [
  article("news-a", ALPHA, "2025-01-03T12:00:00Z"),
  article("news-b", BRAVO, "2025-01-02T12:00:00Z"),
  article("news-c", CHARLIE, "2025-01-01T12:00:00Z"),
];

const publishMock = vi.mocked(intelligenceApi.publishItem);
const sentIds = () => publishMock.mock.calls.map((call) => call[1]);

function queued(id: string) {
  return {
    success: true,
    message: "Queued",
    id,
    title: id,
    published_url: `https://example.test/${id}`,
    published_at: "2025-01-03T12:00:00Z",
    collection: "news",
  };
}

async function openPage() {
  const user = userEvent.setup();
  render(<NewsRoomPage />);
  await screen.findByText(ALPHA);
  return user;
}

type User = ReturnType<typeof userEvent.setup>;

async function select(user: User, ...titles: string[]) {
  for (const title of titles) {
    await user.click(screen.getByRole("button", { name: `Select ${title}` }));
  }
}

async function givePositionToEveryCard(user: User) {
  const pickers = screen
    .getAllByRole("combobox")
    .filter(
      (el) => within(el).queryByRole("option", { name: "Hero Main" }) !== null,
    );
  for (const picker of pickers) await user.selectOptions(picker, "latest");
}

async function search(user: User, text: string) {
  await user.type(screen.getByPlaceholderText(/Search articles/i), text);
}

const publishAll = (user: User) =>
  user.click(screen.getByRole("button", { name: "Publish all" }));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(intelligenceApi.getPendingItems).mockResolvedValue({
    items: ARTICLES,
    count: ARTICLES.length,
  });
  publishMock.mockImplementation(async (_type, id) => queued(id));
});

describe("News Room: Publish all", () => {
  it("sends only the selected cards that are on screen", async () => {
    const user = await openPage();
    await select(user, ALPHA, BRAVO, CHARLIE);
    await givePositionToEveryCard(user);
    await search(user, "Alpha");
    await waitFor(() =>
      expect(screen.queryByText(BRAVO)).not.toBeInTheDocument(),
    );

    await publishAll(user);

    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(sentIds()).toEqual(["news-a"]);
    // The two it did not send are still selected when they come back on screen.
    await user.clear(screen.getByPlaceholderText(/Search articles/i));
    expect(await screen.findByText(/2 selected/)).toBeInTheDocument();
  });

  it("sends nothing when no selected card is on screen, and says so", async () => {
    const user = await openPage();
    await givePositionToEveryCard(user);
    await select(user, ALPHA);
    await search(user, "Bravo");
    await waitFor(() =>
      expect(screen.queryByText(ALPHA)).not.toBeInTheDocument(),
    );

    await publishAll(user);

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(sentIds()).toEqual([]);
    expect(toast.error).toHaveBeenCalledWith(
      "Nothing to publish",
      expect.any(String),
    );
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("does not send again a card that is already queued", async () => {
    const user = await openPage();
    await select(user, ALPHA, BRAVO);
    await givePositionToEveryCard(user);
    // Alpha is queued on its own first; it stays selected.
    await user.click(screen.getAllByRole("button", { name: "Publish" })[0]);
    await screen.findAllByText("Queued");

    await publishAll(user);

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "Bulk publication requests completed",
        expect.any(String),
      ),
    );
    expect(sentIds()).toEqual(["news-a", "news-b"]);
  });

  it("says how many of the selected cards it will send when that is not all of them", async () => {
    const user = await openPage();
    await select(user, ALPHA, BRAVO, CHARLIE);
    expect(screen.queryByText(/to publish/)).not.toBeInTheDocument();

    await search(user, "Alpha");

    expect(await screen.findByText("1 to publish")).toBeInTheDocument();
    expect(screen.getByText(/3 selected/)).toBeInTheDocument();
  });

  it("reports a run where every request failed as a failure, and keeps the cards selected", async () => {
    publishMock.mockRejectedValue(new Error("not allowed"));
    const user = await openPage();
    await select(user, ALPHA, BRAVO);
    await givePositionToEveryCard(user);

    await publishAll(user);

    await waitFor(() => expect(publishMock).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Bulk publication incomplete",
        expect.stringContaining("0 queued, 2 failed"),
      ),
    );
    expect(toast.error).toHaveBeenCalledWith(
      "Bulk publication incomplete",
      expect.stringContaining("not allowed"),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByText(/2 selected/)).toBeInTheDocument();
  });

  it("counts an answer that says 'not queued' as a failure", async () => {
    publishMock.mockResolvedValue({
      ...queued("news-a"),
      success: false,
      message: "Slug already exists",
    });
    const user = await openPage();
    await select(user, ALPHA);
    await givePositionToEveryCard(user);

    await publishAll(user);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Bulk publication incomplete",
        expect.stringContaining("Slug already exists"),
      ),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
    expect(screen.getByText(/1 selected/)).toBeInTheDocument();
  });

  it("keeps selected only what failed when some were queued", async () => {
    publishMock.mockImplementation(async (_type, id) => {
      if (id === "news-b") throw new Error("not allowed");
      return queued(id);
    });
    const user = await openPage();
    await select(user, ALPHA, BRAVO);
    await givePositionToEveryCard(user);

    await publishAll(user);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Bulk publication incomplete",
        expect.stringContaining("1 queued, 1 failed"),
      ),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByText(/1 selected/)).toBeInTheDocument();
    expect(sentIds()).toEqual(["news-a", "news-b"]);
  });

  it("when every request is queued it says so and empties the selection", async () => {
    const user = await openPage();
    await select(user, ALPHA, BRAVO);
    await givePositionToEveryCard(user);

    await publishAll(user);

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "Bulk publication requests completed",
        "2 queued, 0 failed.",
      ),
    );
    expect(toast.error).not.toHaveBeenCalled();
    expect(sentIds()).toEqual(["news-a", "news-b"]);
    await waitFor(() =>
      expect(screen.queryByText(/selected/)).not.toBeInTheDocument(),
    );
  });
});

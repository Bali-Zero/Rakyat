/**
 * The Visa Oracle queue lists what is still in the queue.
 *
 * The list the page loads also carries rows that are already published (the newest
 * of the archive). They were shown as pending, with Approve and Reject buttons that
 * can only fail: their files are no longer where those actions look. A row with no
 * source broke the search, and any text in `source` became a link. Invented rows only.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import VisaOraclePage from "./page";
import { intelligenceApi, type StagingItem } from "@/lib/api/intelligence.api";

vi.mock("@/lib/api/intelligence.api");
vi.mock("@/lib/logger");
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  }),
}));

function row(
  over: Partial<StagingItem> & { id: string; title: string },
): StagingItem {
  return {
    type: "visa",
    status: "pending",
    detected_at: "2025-01-02T11:00:00Z",
    source: "https://example.test/rule",
    detection_type: "NEW",
    ...over,
  };
}

const WAITING = row({ id: "visa-a", title: "Alpha stay permit rule" });
const PUBLISHED = row({
  id: "visa-b",
  title: "Bravo rule already published",
  status: "published",
});

function serve(items: StagingItem[]) {
  vi.mocked(intelligenceApi.getPendingItems).mockResolvedValue({
    items,
    count: items.length,
  });
}

async function loaded() {
  await waitFor(() =>
    expect(
      screen.queryByText("Scanning Intelligence Feed..."),
    ).not.toBeInTheDocument(),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Visa Oracle queue", () => {
  it("does not list a row that is already published", async () => {
    serve([WAITING, PUBLISHED]);
    render(<VisaOraclePage />);

    expect(await screen.findByText(WAITING.title)).toBeInTheDocument();
    expect(screen.queryByText(PUBLISHED.title)).not.toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /Approve & Ingest/ }),
    ).toHaveLength(1);
    expect(screen.getByText(/1 of 1 pending/)).toBeInTheDocument();
  });

  it("offers no Approve or Reject when every row is already published", async () => {
    serve([PUBLISHED]);
    render(<VisaOraclePage />);

    await loaded();
    expect(screen.queryByText(PUBLISHED.title)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Reject/ })).toBeNull();
  });

  it("searches a list in which a row has no source", async () => {
    serve([
      WAITING,
      row({
        id: "visa-c",
        title: "Charlie rule with no source",
        source: null as unknown as string,
      }),
    ]);
    const user = userEvent.setup();
    render(<VisaOraclePage />);
    await screen.findByText(WAITING.title);

    // "alpha" is in neither the title nor the id of the row without a source,
    // so the search has to look at that row's source.
    await user.type(screen.getByLabelText("Search visa oracle"), "alpha");

    await waitFor(() =>
      expect(
        screen.queryByText("Charlie rule with no source"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText(WAITING.title)).toBeInTheDocument();
  });

  it.each([
    ["no source", null],
    ["an empty source", ""],
    ["a blank source", "   "],
  ])("shows no source at all for %s", async (_label, source) => {
    serve([
      row({
        id: "visa-d",
        title: "Delta rule",
        source: source as unknown as string,
      }),
    ]);
    render(<VisaOraclePage />);

    await screen.findByText("Delta rule");
    expect(screen.queryByRole("link", { name: /Source/ })).toBeNull();
    expect(screen.queryByText(/Source/)).toBeNull();
  });

  it.each([
    ["a script address", "javascript:void(0)"],
    ["a bare word", "ministry circular"],
    ["an address with no scheme", "//example.test/rule"],
    ["markup", "<b>circular</b>"],
  ])("shows %s as plain text, never as a link", async (_label, source) => {
    serve([row({ id: "visa-d", title: "Delta rule", source })]);
    render(<VisaOraclePage />);

    await screen.findByText("Delta rule");
    expect(screen.queryByRole("link", { name: /Source/ })).toBeNull();
    // The operator still reads where the row came from.
    expect(screen.getByText(`Source: ${source}`)).toBeVisible();
    expect(document.querySelector(`a[href="${source}"]`)).toBeNull();
  });

  it("keeps the Source link of a web address and both pending rows", async () => {
    serve([
      WAITING,
      row({
        id: "visa-e",
        title: "Echo rule",
        source: "http://example.test/echo",
      }),
    ]);
    render(<VisaOraclePage />);

    await screen.findByText("Echo rule");
    const links = screen.getAllByRole("link", { name: /Source/ });
    expect(links.map((a) => a.getAttribute("href")).sort()).toEqual([
      "http://example.test/echo",
      "https://example.test/rule",
    ]);
    expect(screen.getByText(/2 of 2 pending/)).toBeInTheDocument();
  });
});

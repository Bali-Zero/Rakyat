/**
 * Review — document requests: the page of the card reviewers. The test mocks `@/lib/api`
 * (`api.get`, `api.post`), not the hook, and keeps the real ApiError. Names are invented.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/error-handler";
import type {
  DocumentRequestReviewItem,
  DocumentRequestReviewList,
} from "@/lib/types/document-requests";

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: apiMock };
});

import DocumentRequestReviewPage from "./page";

const BASE = "/api/crm/practices/document-requests";
const PENDING_URL = `${BASE}/review?state=pending&limit=50`;
const DECIDED_URL = `${BASE}/review?state=decided&limit=20`;

function item(
  over: Partial<DocumentRequestReviewItem> = {},
): DocumentRequestReviewItem {
  return {
    id: 77,
    practice_id: 2318,
    service_name: "Zz Service",
    document_type: "proof_of_accommodation",
    label: "Proof of accommodation",
    client_text: "Your booking for the first nights.",
    reason: "Stays with a friend, not in a hotel.",
    requested_by: "creator@example.test",
    requested_at: "2026-11-28T09:30:00+00:00",
    seen_count: 0,
    card_state: "can_add",
    card_live: true,
    decision: "pending",
    decision_condition: null,
    decided_by: null,
    decided_at: null,
    ...over,
  };
}

function list(
  items: DocumentRequestReviewItem[],
  total = items.length,
): DocumentRequestReviewList {
  return { items, total };
}

function serve(
  pending: DocumentRequestReviewItem[],
  decided: DocumentRequestReviewItem[] = [],
) {
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === PENDING_URL) return list(pending);
    if (url === DECIDED_URL) return list(decided);
    throw new Error(`unexpected GET ${url}`);
  });
}

function refusal(code: string) {
  return new ApiError("Conflict", 409, { detail: { code, message: "x" } });
}

async function open(
  pending: DocumentRequestReviewItem[],
  decided: DocumentRequestReviewItem[] = [],
) {
  serve(pending, decided);
  render(<DocumentRequestReviewPage />);
  await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith(PENDING_URL));
}

beforeEach(() => {
  apiMock.get.mockReset();
  apiMock.post.mockReset();
});
afterEach(cleanup);

describe("review of the document requests", () => {
  it("shows only one sentence to a visitor who is not a reviewer, with one call", async () => {
    apiMock.get.mockRejectedValue(new ApiError("Forbidden", 403));
    render(<DocumentRequestReviewPage />);
    expect(
      await screen.findByText("Only card reviewers see this page."),
    ).toBeTruthy();
    expect(screen.queryByText("Asked outside the card")).toBeNull();
    expect(screen.queryByText("Decided")).toBeNull();
    expect(apiMock.get).toHaveBeenCalledTimes(1);
  });

  it("says there is nothing to decide, and hides Decided when empty", async () => {
    await open([]);
    expect(await screen.findByText("Nothing to decide.")).toBeTruthy();
    expect(screen.getByText("0 to decide")).toBeTruthy();
    expect(screen.getByText("0 requests, newest first")).toBeTruthy();
    expect(screen.queryByText("Decided")).toBeNull();
    expect(apiMock.get).toHaveBeenCalledWith(DECIDED_URL);
  });

  it("shows the title, the rules and one card per request", async () => {
    await open([item()]);
    expect(await screen.findByText("Review — document requests")).toBeTruthy();
    expect(screen.getByText("1 request, newest first")).toBeTruthy();
    expect(screen.getByText("1 to decide")).toBeTruthy();
    expect(screen.getByText("Proof of accommodation")).toBeTruthy();
    expect(screen.getByText("Zz Service")).toBeTruthy();
    expect(screen.getByText("Creator · 28 Nov")).toBeTruthy();
    expect(
      screen.getByText("Stays with a friend, not in a hotel."),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "#2318" }).getAttribute("href"),
    ).toBe("/process/2318");
    expect(
      screen.getByText(
        "Every decision is recorded with your name and the date. Reopening one clears it.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /export/i })).toBeNull();
  });

  it.each([
    [0, "First time"],
    [1, "2nd time in 90 days"],
    [2, "3rd time in 90 days"],
    [10, "11th time in 90 days"],
    [20, "21st time in 90 days"],
  ])("writes seen_count %i as %s", async (seen, text) => {
    await open([item({ seen_count: seen })]);
    expect(await screen.findByText(text)).toBeTruthy();
  });

  it("posts Always with the text as typed, and loads both lists again", async () => {
    await open([item()]);
    apiMock.post.mockResolvedValue({ id: 77 });
    fireEvent.change(await screen.findByLabelText("What the client reads"), {
      target: { value: "  Edited text  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add to the card" }));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(`${BASE}/77/decision`, {
        decision: "added_to_card",
        condition: null,
        client_text: "Edited text",
      }),
    );
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledTimes(4));
  });

  it("posts Only when… with its condition, and blocks while a field is blank", async () => {
    await open([item()]);
    apiMock.post.mockResolvedValue({ id: 77 });
    fireEvent.click(await screen.findByRole("button", { name: "Only when…" }));
    const add = screen.getByRole("button", { name: "Add to the card" });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("When"), {
      target: { value: "  the client stays with a friend  " },
    });
    expect((add as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(screen.getByLabelText("What the client reads"), {
      target: { value: "   " },
    });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("What the client reads"), {
      target: { value: "A letter from your host." },
    });
    fireEvent.click(add);
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(`${BASE}/77/decision`, {
        decision: "added_to_card",
        condition: "the client stays with a friend",
        client_text: "A letter from your host.",
      }),
    );
  });

  it("blocks Always while the text is blank", async () => {
    await open([item()]);
    fireEvent.change(await screen.findByLabelText("What the client reads"), {
      target: { value: "" },
    });
    const add = screen.getByRole("button", { name: "Add to the card" });
    expect((add as HTMLButtonElement).disabled).toBe(true);
  });

  it("tells the truth under the buttons for each card_live and choice", async () => {
    await open([item({ card_live: true })]);
    expect(
      await screen.findByText(
        "New Zz Service practices ask for it from now on. Open practices are not changed.",
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Only when…" }));
    expect(
      screen.getByText(
        "It is saved on the Zz Service card with its condition. It is not asked automatically. Open practices are not changed.",
      ),
    ).toBeTruthy();
    cleanup();

    await open([item({ card_live: false })]);
    expect(
      await screen.findByText(
        "It is saved on the Zz Service card. New practices will ask for it once that card is switched on. Open practices are not changed.",
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Only when…" }));
    expect(
      screen.getByText(
        "It is saved on the Zz Service card with its condition. It is not asked automatically. Open practices are not changed.",
      ),
    ).toBeTruthy();
  });

  it("offers only Only this case when there is no card, and posts it", async () => {
    await open([item({ card_state: "no_card" })]);
    apiMock.post.mockResolvedValue({ id: 77 });
    expect(await screen.findByText("No card for this service")).toBeTruthy();
    expect(
      screen.getByText("This service has no card to add to."),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Add to the card" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Only this case" }));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(`${BASE}/77/decision`, {
        decision: "case_only",
      }),
    );
  });

  it("says so when the practice has no service", async () => {
    await open([item({ card_state: "no_card", service_name: null })]);
    expect(await screen.findAllByText("No service")).toHaveLength(2);
    expect(
      screen.getByText(
        "This practice has no service, so there is no card to add to.",
      ),
    ).toBeTruthy();
    expect(
      screen.getAllByRole("button", { name: "Only this case" }),
    ).toHaveLength(1);
  });

  it("offers only Only this case when the document is already on the card", async () => {
    await open([item({ card_state: "on_card" })]);
    apiMock.post.mockResolvedValue({ id: 77 });
    expect(await screen.findByText("Already on the card")).toBeTruthy();
    expect(
      screen.getByText("This document is already on the Zz Service card."),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Add to the card" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Only this case" }));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(`${BASE}/77/decision`, {
        decision: "case_only",
      }),
    );
  });

  it.each([
    ["already_decided", "Someone already decided this one."],
    ["already_on_card", "This document is already on the card."],
    ["no_card", "This service has no card."],
    ["not_decided", "This one is not decided."],
    ["something_else", "Could not save. Try again."],
  ])("shows the message of %s and keeps what was typed", async (code, text) => {
    await open([item()]);
    apiMock.post.mockRejectedValue(refusal(code));
    fireEvent.click(await screen.findByRole("button", { name: "Only when…" }));
    fireEvent.change(screen.getByLabelText("When"), {
      target: { value: "the sponsor is a company" },
    });
    fireEvent.change(screen.getByLabelText("What the client reads"), {
      target: { value: "Typed by the reviewer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add to the card" }));
    expect(await screen.findByText(text)).toBeTruthy();
    expect((screen.getByLabelText("When") as HTMLInputElement).value).toBe(
      "the sponsor is a company",
    );
    expect(
      (screen.getByLabelText("What the client reads") as HTMLTextAreaElement)
        .value,
    ).toBe("Typed by the reviewer");
    expect(apiMock.get).toHaveBeenCalledTimes(2);
  });

  it("shows Could not save for an error that is not an ApiError", async () => {
    await open([item({ card_state: "on_card" })]);
    apiMock.post.mockRejectedValue(new Error("network down"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Only this case" }),
    );
    expect(await screen.findByText("Could not save. Try again.")).toBeTruthy();
  });

  it("lists the decided with their three outcomes", async () => {
    await open(
      [],
      [
        item({
          id: 1,
          label: "Doc one",
          decision: "added_to_card",
          decided_by: "boss@example.test",
          decided_at: "2026-11-27T10:00:00+00:00",
        }),
        item({
          id: 2,
          label: "Doc two",
          decision: "added_to_card",
          decision_condition: "the sponsor is a company",
          decided_by: "boss@example.test",
          decided_at: "2026-11-26T10:00:00+00:00",
        }),
        item({
          id: 3,
          label: "Doc three",
          decision: "case_only",
          decided_by: "chief@example.test",
          decided_at: "2026-11-05T10:00:00+00:00",
        }),
      ],
    );
    expect(await screen.findByText("Decided")).toBeTruthy();
    expect(screen.getByText("Added to the card")).toBeTruthy();
    expect(
      screen.getByText("Added to the card, only when the sponsor is a company"),
    ).toBeTruthy();
    expect(screen.getByText("Only this case")).toBeTruthy();
    expect(screen.getByText("Boss · 27 Nov")).toBeTruthy();
    expect(screen.getByText("Chief · 5 Nov")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Reopen" })).toHaveLength(3);
  });

  it("reopens with a post to the reopen URL and loads both lists again", async () => {
    await open(
      [],
      [
        item({
          id: 9,
          decision: "case_only",
          decided_by: "boss@example.test",
          decided_at: "2026-11-27T10:00:00+00:00",
        }),
      ],
    );
    apiMock.post.mockResolvedValue({
      id: 9,
      decision: "pending",
      card_document_removed: false,
    });
    fireEvent.click(await screen.findByRole("button", { name: "Reopen" }));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(`${BASE}/9/reopen`),
    );
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledTimes(4));
    expect(apiMock.get.mock.calls.map((call) => call[0])).toEqual([
      PENDING_URL,
      DECIDED_URL,
      PENDING_URL,
      DECIDED_URL,
    ]);
  });

  it("shows the message of a refused reopen", async () => {
    await open(
      [],
      [
        item({
          id: 9,
          decision: "case_only",
          decided_by: "boss@example.test",
          decided_at: "2026-11-27T10:00:00+00:00",
        }),
      ],
    );
    apiMock.post.mockRejectedValue(refusal("not_decided"));
    fireEvent.click(await screen.findByRole("button", { name: "Reopen" }));
    const line = (await screen.findByText("This one is not decided.")).closest(
      "li",
    );
    expect(line).toBeTruthy();
    expect(within(line as HTMLElement).getByText("Reopen")).toBeTruthy();
  });
});

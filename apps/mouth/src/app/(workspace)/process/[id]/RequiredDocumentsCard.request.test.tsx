/**
 * V2: the add dialog asks the client for one more document and says why. It posts to
 * /document-requests (not the old required-documents endpoint); the 409 messages come from
 * `detail.code`. The test mocks `@/lib/api`, not the hook, and keeps the real ApiError.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/error-handler";
import type { RequiredDocument } from "@/lib/types/required-documents";

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: apiMock };
});

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { dismiss: vi.fn() }),
}));

import { RequiredDocumentsCard } from "./RequiredDocumentsCard";

const URL = "/api/crm/practices/1324/document-requests";

function created(over: Partial<RequiredDocument> = {}): RequiredDocument {
  return {
    id: 901,
    practice_id: 1324,
    document_type: "proof_of_accommodation",
    document_label: "Proof of accommodation",
    description: "Your booking for the first nights.",
    is_required: true,
    uploaded_by_client: false,
    uploaded_file_id: null,
    uploaded_at: null,
    client_notes: null,
    team_member_notes: null,
    status: "pending",
    created_at: "2026-11-28T09:30:00Z",
    updated_at: "2026-11-28T09:30:00Z",
    ...over,
  };
}

async function openDialog() {
  render(<RequiredDocumentsCard practiceId={1324} />);
  fireEvent.click(await screen.findByRole("button", { name: /^add$/i }));
  return screen.getByRole("dialog");
}

function fill(name: string, text: string, reason: string) {
  fireEvent.change(screen.getByLabelText("Document"), {
    target: { value: name },
  });
  fireEvent.change(screen.getByLabelText("What the client reads"), {
    target: { value: text },
  });
  fireEvent.change(screen.getByLabelText("Why this case needs it"), {
    target: { value: reason },
  });
}

const submit = () =>
  screen.getByRole("button", {
    name: /ask for document/i,
  }) as HTMLButtonElement;

describe("RequiredDocumentsCard ask-for-a-document dialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.get.mockResolvedValue([]);
  });
  afterEach(cleanup);

  it("shows a free-text Document with suggestions, the client text, the reason and its hint, and no Required checkbox", async () => {
    await openDialog();
    const name = screen.getByLabelText("Document") as HTMLInputElement;
    expect(name.required).toBe(true);
    expect(name.getAttribute("list")).toBe("request-document-suggestions");
    expect(name.maxLength).toBe(100);
    const suggestions = Array.from(
      document.querySelectorAll("#request-document-suggestions option"),
    ).map((o) => (o as HTMLOptionElement).value);
    expect(suggestions).toContain("CV / Resume");
    expect(suggestions).not.toContain("Other");
    const text = screen.getByLabelText(
      "What the client reads",
    ) as HTMLTextAreaElement;
    const reason = screen.getByLabelText(
      "Why this case needs it",
    ) as HTMLTextAreaElement;
    expect(text.required && reason.required).toBe(true);
    expect([text.maxLength, reason.maxLength]).toEqual([2000, 1000]);
    expect(
      screen.getByText("Do not write the client's name here."),
    ).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByLabelText(/required document/i)).toBeNull();
  });

  it("blocks the submit without a reason", async () => {
    await openDialog();
    fill("Proof of accommodation", "Your booking.", "   ");
    expect(submit().disabled).toBe(true);
    fireEvent.click(submit());
    expect(apiMock.post).not.toHaveBeenCalled();
  });

  it("blocks the submit without a client text, after trim", async () => {
    await openDialog();
    fill("Proof of accommodation", " \n ", "Stays with a friend.");
    expect(submit().disabled).toBe(true);
    fireEvent.click(submit());
    expect(apiMock.post).not.toHaveBeenCalled();
  });

  it("posts name, client text and reason to the new endpoint and shows the new row", async () => {
    apiMock.post.mockResolvedValue({ request_id: 77, document: created() });
    await openDialog();
    fill(
      "  Proof of accommodation ",
      "Your booking for the first nights.",
      "Stays with a friend.",
    );
    fireEvent.click(submit());
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledTimes(1));
    const [url, body] = apiMock.post.mock.calls[0];
    expect(url).toBe(URL);
    expect(Object.keys(body).sort()).toEqual([
      "client_text",
      "document_label",
      "reason",
    ]);
    expect(body).toEqual({
      document_label: "Proof of accommodation",
      client_text: "Your booking for the first nights.",
      reason: "Stays with a friend.",
    });
    expect(await screen.findByText("Proof of accommodation")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("sends the option's value as document_type when the name equals a suggestion, and none otherwise", async () => {
    apiMock.post.mockResolvedValue({
      request_id: 78,
      document: created({
        id: 902,
        document_type: "cv",
        document_label: "CV / Resume",
      }),
    });
    await openDialog();
    fill("CV / Resume", "Your CV.", "Needed for this case.");
    fireEvent.click(submit());
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledTimes(1));
    expect(apiMock.post.mock.calls[0][1]).toEqual({
      document_label: "CV / Resume",
      client_text: "Your CV.",
      reason: "Needed for this case.",
      document_type: "cv",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    apiMock.post.mockResolvedValue({
      request_id: 79,
      document: created({ id: 903 }),
    });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
    fill("Proof of accommodation", "Your booking.", "Stays with a friend.");
    fireEvent.click(submit());
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledTimes(2));
    expect("document_type" in apiMock.post.mock.calls[1][1]).toBe(false);
  });

  it.each([
    ["already_on_practice", /already on this practice/i],
    ["practice_closed", /this practice is closed/i],
  ])(
    "a 409 %s shows a plain message and keeps the dialog open",
    async (code, message) => {
      apiMock.post.mockRejectedValue(
        new ApiError("conflict", 409, {
          detail: { code, message: "server text" },
        }),
      );
      await openDialog();
      fill("Proof of accommodation", "Your booking.", "Stays with a friend.");
      fireEvent.click(submit());
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toMatch(message);
      expect(screen.getByRole("dialog")).toBeTruthy();
      expect(
        (screen.getByLabelText("Document") as HTMLInputElement).value,
      ).toBe("Proof of accommodation");
    },
  );

  it.each([
    [403, /do not have permission/i],
    [422, /check the fields/i],
  ])(
    "a %s shows a generic plain message instead of failing silently",
    async (status, message) => {
      apiMock.post.mockRejectedValue(new ApiError("refused", status));
      await openDialog();
      fill("Proof of accommodation", "Your booking.", "Stays with a friend.");
      fireEvent.click(submit());
      expect((await screen.findByRole("alert")).textContent).toMatch(message);
      expect(screen.getByRole("dialog")).toBeTruthy();
    },
  );
});

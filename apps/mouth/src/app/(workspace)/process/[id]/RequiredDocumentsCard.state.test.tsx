/**
 * V2, third cut: a row says where its request stands, and the consultant can correct the client
 * text from it. The test mocks `@/lib/api` (get / post / patch), not the hook, and keeps the real
 * ApiError. Names and addresses are invented.
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
import type {
  DocumentRequestState,
  PracticeDocumentRequest,
  RequiredDocument,
} from "@/lib/types/required-documents";

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}));
const toastMock = vi.hoisted(() => ({
  fn: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  dismiss: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: apiMock };
});

vi.mock("sonner", () => ({
  toast: Object.assign(toastMock.fn, {
    success: toastMock.success,
    error: toastMock.error,
    dismiss: toastMock.dismiss,
  }),
}));

import { RequiredDocumentsCard } from "./RequiredDocumentsCard";

const DOCS_URL = "/api/crm/practices/1324/required-documents";
const STATES_URL = "/api/crm/practices/1324/document-requests";
const CORRECT = "Correct the text for the client";
const NOTE =
  "The client sees the new text in the portal at once; nothing is sent to them. The reviewers are not emailed again.";
const CARD_SENTENCE =
  "The service card keeps its own text: only this practice changes.";

function slot(over: Partial<RequiredDocument> = {}): RequiredDocument {
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

function request(
  state: DocumentRequestState = "pending",
  over: Partial<PracticeDocumentRequest> = {},
): PracticeDocumentRequest {
  return {
    id: 77,
    required_document_id: 901,
    state,
    requested_by: "dana@example.test",
    requested_at: "2026-11-28T09:30:00Z",
    ...over,
  };
}

let documents: RequiredDocument[];
let states: PracticeDocumentRequest[];

function serve() {
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === DOCS_URL) return documents;
    if (url === STATES_URL) return states;
    throw new Error(`unexpected GET ${url}`);
  });
}

const statesCalls = () =>
  apiMock.get.mock.calls.filter((call) => call[0] === STATES_URL);

async function renderCard(readOnly = false) {
  render(<RequiredDocumentsCard practiceId={1324} readOnly={readOnly} />);
  await screen.findByText("Proof of accommodation");
}

async function openCorrection() {
  await renderCard();
  fireEvent.click(await screen.findByRole("button", { name: CORRECT }));
  return screen.getByRole("dialog");
}

const textbox = () =>
  screen.getByLabelText("What the client reads") as HTMLTextAreaElement;
const save = () =>
  screen.getByRole("button", { name: "Save text" }) as HTMLButtonElement;

describe("RequiredDocumentsCard request state and text correction", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    documents = [slot()];
    states = [request()];
    serve();
  });
  afterEach(cleanup);

  it.each([
    ["pending", "Added by Dana · 28 Nov · waiting for the card review"],
    ["added_to_card", "Added by Dana · 28 Nov · now on the card"],
    ["case_only", "Added by Dana · 28 Nov · this case only"],
  ] as const)("shows the line of a %s request", async (state, line) => {
    states = [request(state)];
    await renderCard();
    expect(await screen.findByText(line)).toBeTruthy();
  });

  it("shows no line and no button on a slot with no request", async () => {
    states = [];
    await renderCard();
    await waitFor(() => expect(statesCalls()).toHaveLength(1));
    expect(screen.queryByText(/Added by/)).toBeNull();
    expect(screen.queryByRole("button", { name: CORRECT })).toBeNull();
  });

  it("asks for the states only after the documents answered", async () => {
    let release: (docs: RequiredDocument[]) => void = () => {};
    apiMock.get.mockImplementation((url: string) => {
      if (url === DOCS_URL)
        return new Promise<RequiredDocument[]>((resolve) => {
          release = resolve;
        });
      return Promise.resolve(states);
    });
    render(<RequiredDocumentsCard practiceId={1324} />);
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith(DOCS_URL));
    expect(statesCalls()).toHaveLength(0);
    release(documents);
    await waitFor(() => expect(statesCalls()).toHaveLength(1));
    expect(await screen.findByText(/Added by Dana/)).toBeTruthy();
  });

  it("never asks for the states when the documents were refused", async () => {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === DOCS_URL) throw new ApiError("Not found", 404, {});
      return states;
    });
    render(<RequiredDocumentsCard practiceId={1324} />);
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith(DOCS_URL));
    await waitFor(() =>
      expect(screen.queryByText(/Loading documents/)).toBeNull(),
    );
    expect(statesCalls()).toHaveLength(0);
  });

  it("keeps the rows and shows no toast when the states call fails", async () => {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === DOCS_URL) return documents;
      throw new ApiError("Server error", 500, {});
    });
    await renderCard();
    await waitFor(() => expect(statesCalls()).toHaveLength(1));
    expect(screen.getByText("Proof of accommodation")).toBeTruthy();
    expect(screen.queryByText(/Added by/)).toBeNull();
    expect(toastMock.fn).not.toHaveBeenCalled();
    expect(toastMock.error).not.toHaveBeenCalled();
  });

  it("shows the line and no button when the card is read only", async () => {
    await renderCard(true);
    expect(await screen.findByText(/Added by Dana/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: CORRECT })).toBeNull();
  });

  it.each(["uploaded", "verified"] as const)(
    "shows the line and no button on a %s slot",
    async (status) => {
      documents = [slot({ status })];
      await renderCard();
      expect(await screen.findByText(/Added by Dana/)).toBeTruthy();
      expect(screen.queryByRole("button", { name: CORRECT })).toBeNull();
    },
  );

  it("offers the button on a rejected slot", async () => {
    documents = [slot({ status: "rejected" })];
    await renderCard();
    expect(await screen.findByRole("button", { name: CORRECT })).toBeTruthy();
  });

  it("opens the dialog with the current text and the note, without the card sentence for a pending request", async () => {
    const dialog = await openCorrection();
    expect(dialog.textContent).toContain(CORRECT);
    expect(dialog.textContent).toContain(NOTE);
    expect(dialog.textContent).not.toContain(CARD_SENTENCE);
    expect(textbox().value).toBe("Your booking for the first nights.");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });

  it("adds the sentence about the service card for an added_to_card request", async () => {
    states = [request("added_to_card")];
    const dialog = await openCorrection();
    expect(dialog.textContent).toContain(NOTE);
    expect(dialog.textContent).toContain(CARD_SENTENCE);
  });

  it("keeps Save text disabled while the text is blank or unchanged", async () => {
    await openCorrection();
    expect(save().disabled).toBe(true);
    fireEvent.change(textbox(), { target: { value: "   \n " } });
    expect(save().disabled).toBe(true);
    fireEvent.change(textbox(), {
      target: { value: "  Your booking for the first nights.  " },
    });
    expect(save().disabled).toBe(true);
    fireEvent.change(textbox(), { target: { value: "A clearer text." } });
    expect(save().disabled).toBe(false);
  });

  it("patches the trimmed text, shows it on the row and closes the dialog", async () => {
    apiMock.patch.mockResolvedValue({
      request_id: 77,
      document: slot({ description: "A clearer text." }),
    });
    await openCorrection();
    fireEvent.change(textbox(), { target: { value: "  A clearer text.  " } });
    fireEvent.click(save());
    await waitFor(() =>
      expect(apiMock.patch).toHaveBeenCalledWith(`${STATES_URL}/77`, {
        client_text: "A clearer text.",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("A clearer text.")).toBeTruthy();
    expect(toastMock.success).toHaveBeenCalledWith("Text corrected");
  });

  it.each([
    [
      409,
      "practice_closed",
      "This practice is closed: its documents can no longer be changed.",
    ],
    [409, "slot_withdrawn", "This document was removed from the practice."],
    [
      409,
      "slot_answered",
      "The client already sent this document: mark it rejected first.",
    ],
    [404, "request_not_found", "This request no longer exists."],
  ] as const)(
    "shows the message of %s %s and keeps the typed text",
    async (status, code, message) => {
      apiMock.patch.mockRejectedValue(
        new ApiError(message, status, { detail: { code, message: "x" } }),
      );
      await openCorrection();
      fireEvent.change(textbox(), { target: { value: "My typed text." } });
      fireEvent.click(save());
      expect((await screen.findByRole("alert")).textContent).toBe(message);
      expect(screen.getByRole("dialog")).toBeTruthy();
      expect(textbox().value).toBe("My typed text.");
    },
  );

  it("cannot be dismissed while the text is being sent, so a late failure keeps what was typed", async () => {
    let fail: (reason: unknown) => void = () => {};
    apiMock.patch.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          fail = reject;
        }),
    );
    const dialog = await openCorrection();
    fireEvent.change(textbox(), { target: { value: "My typed text." } });
    fireEvent.click(save());
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalledTimes(1));
    fireEvent.keyDown(dialog, { key: "Escape", code: "Escape" });
    expect(screen.getByRole("dialog")).toBeTruthy();

    fail(
      new ApiError("closed", 409, {
        detail: { code: "practice_closed", message: "x" },
      }),
    );
    expect((await screen.findByRole("alert")).textContent).toBe(
      "This practice is closed: its documents can no longer be changed.",
    );
    expect(textbox().value).toBe("My typed text.");
  });

  it("shows the 403 message and, for anything else, the generic one", async () => {
    apiMock.patch.mockRejectedValueOnce(new ApiError("Forbidden", 403, {}));
    await openCorrection();
    fireEvent.change(textbox(), { target: { value: "My typed text." } });
    fireEvent.click(save());
    expect((await screen.findByRole("alert")).textContent).toBe(
      "You cannot change documents on this practice.",
    );
    expect(textbox().value).toBe("My typed text.");

    apiMock.patch.mockRejectedValueOnce(new ApiError("Boom", 500, {}));
    fireEvent.click(save());
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe(
        "Could not save the text. Try again.",
      ),
    );
    expect(textbox().value).toBe("My typed text.");
  });

  it("asks for the states again after Ask for a document and shows the pending line of the new row", async () => {
    await renderCard();
    await waitFor(() => expect(statesCalls()).toHaveLength(1));
    const added = slot({
      id: 902,
      document_type: "flight_ticket",
      document_label: "Flight ticket",
      description: "Your flight booking.",
    });
    apiMock.post.mockResolvedValue({ request_id: 78, document: added });
    states = [
      request("pending", { id: 78, required_document_id: 902 }),
      request("case_only"),
    ];
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
    fireEvent.change(screen.getByLabelText("Document"), {
      target: { value: "Flight ticket" },
    });
    fireEvent.change(screen.getByLabelText("What the client reads"), {
      target: { value: "Your flight booking." },
    });
    fireEvent.change(screen.getByLabelText("Why this case needs it"), {
      target: { value: "Arrives by air." },
    });
    fireEvent.click(screen.getByRole("button", { name: /ask for document/i }));
    await waitFor(() => expect(statesCalls()).toHaveLength(2));
    expect(await screen.findByText("Flight ticket")).toBeTruthy();
    expect(
      await screen.findByText(
        "Added by Dana · 28 Nov · waiting for the card review",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/this case only/)).toBeTruthy();
  });
});

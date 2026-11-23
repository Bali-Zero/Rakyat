import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Practice } from "@/lib/api/crm/crm.types";
import { FinalDeliveryCard } from "./FinalDeliveryCard";
import { workDayOf } from "@/lib/format/work-day";

const requests = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  sentSub: "staff-a",
  identity: { sub: "staff-a", epoch: 1 },
}));
vi.mock("@/lib/auth/session-identity", () => ({
  useSessionIdentity: () => requests.identity,
  readSessionIdentity: () => requests.identity,
  isSameIdentity: (a: { sub: string }, b: { sub: string }) => a.sub === b.sub,
  acceptResponse: (res: { sentSub: string }, who: { sub: string }) =>
    res.sentSub === who.sub,
}));
vi.mock("@/lib/api", () => ({
  api: {
    requestWithCredential: async (
      path: string,
      opts?: { method?: string; body?: string },
    ) => {
      const data =
        opts?.method === "POST"
          ? await (opts.body
              ? requests.post(path, JSON.parse(opts.body))
              : requests.post(path))
          : await requests.get(path);
      return { data, sentSub: requests.sentSub };
    },
  },
}));

const base = "/api/crm/practices/12/delivery";
const slot = (
  key: string,
  label: string,
  extras: Record<string, unknown> = {},
) => ({
  key,
  label,
  condition: null,
  document_id: null,
  file_name: null,
  waiver_reason: null,
  ...extras,
});
const requiredSlots = [
  slot("electronic_visa", "Electronic Visa"),
  slot("itk", "ITK — Visit Stay Permit"),
];
const initial = {
  status: "on_process",
  needs_titles: false,
  bonus_slot: "electronic_visa",
  bonus_event: null,
  slots: requiredSlots,
};

function setup(payment = "paid") {
  const complete = vi.fn();
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <FinalDeliveryCard
        practice={
          { id: 12, client_id: 19, payment_status: payment } as Practice
        }
        onComplete={complete}
      />
    </SWRConfig>,
  );
  return { complete, user: userEvent.setup() };
}

describe("Final delivery", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    requests.sentSub = "staff-a";
    requests.get.mockResolvedValue(initial);
  });
  it("records issuance on upload and keeps completion unavailable until the post-entry permit is saved", async () => {
    const [year, month] = workDayOf().split("-").map(Number);
    const printedDate = new Date(Date.UTC(year, month - 1, 0))
      .toISOString()
      .slice(0, 10);
    const issuedVisa = slot("electronic_visa", "Electronic Visa", {
      document_id: 21,
      file_name: "visa.pdf",
      issued_on: printedDate,
    });
    const issued = {
      ...initial,
      bonus_event: "electronic_visa_issued",
      slots: [issuedVisa, requiredSlots[1]],
    };
    const ready = {
      ...issued,
      slots: [
        issuedVisa,
        slot("itk", "ITK — Visit Stay Permit", {
          document_id: 35,
          file_name: "stay.pdf",
        }),
      ],
    };
    requests.post
      .mockResolvedValueOnce(issued)
      .mockResolvedValueOnce(ready)
      .mockResolvedValueOnce({ ...ready, status: "completed" });
    const { complete, user } = setup();
    const deliver = await screen.findByRole("button", {
      name: "Deliver and complete",
    });
    expect(deliver).toBeDisabled();
    const date = screen.getByLabelText("Electronic Visa issue date");
    expect(date).toHaveValue("");
    expect(screen.getByLabelText("Upload Electronic Visa")).toBeDisabled();
    fireEvent.change(date, { target: { value: "2999-12-01" } });
    expect(screen.getByLabelText("Upload Electronic Visa")).toBeDisabled();
    expect(requests.post).not.toHaveBeenCalled();
    fireEvent.change(date, { target: { value: printedDate } });
    await user.upload(
      screen.getByLabelText("Upload Electronic Visa"),
      new File(["issued"], "visa.pdf", { type: "application/pdf" }),
    );
    await screen.findByText(
      "Bonus recorded on the Electronic Visa upload. Remaining documents do not earn another bonus.",
    );
    expect(requests.post).toHaveBeenCalledWith(
      base + "/slots/electronic_visa",
      {
        file_name: "visa.pdf",
        file_content: "aXNzdWVk",
        issued_on: printedDate,
        mime_type: "application/pdf",
      },
    );
    expect(deliver).toBeDisabled();
    await user.upload(
      screen.getByLabelText("Upload ITK — Visit Stay Permit"),
      new File(["stay"], "stay.pdf", { type: "application/pdf" }),
    );
    await waitFor(() => expect(deliver).toBeEnabled());
    expect(complete).not.toHaveBeenCalled();
    await user.click(deliver);
    await waitFor(() => expect(complete).toHaveBeenCalledOnce());
    expect(requests.post.mock.calls.map((c) => c[0])).toEqual([
      base + "/slots/electronic_visa",
      base + "/slots/itk",
      base + "/complete",
    ]);
  });
  it("preserves the accrued issue date when replacing the Electronic Visa", async () => {
    const recorded = {
      ...initial,
      bonus_event: "electronic_visa_issued",
      slots: [
        slot("electronic_visa", "Electronic Visa", {
          document_id: 21,
          file_name: "visa.pdf",
          issued_on: "2026-02-20",
        }),
        requiredSlots[1],
      ],
    };
    requests.get.mockResolvedValue(recorded);
    requests.post.mockResolvedValueOnce(recorded);
    const { user } = setup();
    expect(await screen.findByText("2026-02-20")).toBeVisible();
    expect(screen.queryByLabelText("Electronic Visa issue date")).toBeNull();
    await user.upload(
      screen.getByLabelText("Replace Electronic Visa"),
      new File(["corrected"], "visa.pdf", { type: "application/pdf" }),
    );
    await waitFor(() => expect(requests.post).toHaveBeenCalledOnce());
    expect(requests.post.mock.calls[0][1]).not.toHaveProperty("issued_on");
  });

  it("cannot revise or deliver from an unsettled practice", async () => {
    setup("partial");
    await screen.findByText("Electronic Visa");
    expect(screen.queryByLabelText("Upload Electronic Visa")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Deliver and complete" }),
    ).toBeNull();
  });
  it("keeps a conditional requirement open until the server accepts its documented waiver", async () => {
    const conditional = slot("filing", "Filing receipt", {
      condition: "If filing is due",
    });
    requests.get.mockResolvedValue({
      ...initial,
      bonus_slot: null,
      slots: [conditional],
    });
    requests.post.mockResolvedValueOnce({
      ...initial,
      bonus_slot: null,
      slots: [{ ...conditional, waiver_reason: "No filing due" }],
    });
    const { user } = setup();
    const deliver = await screen.findByRole("button", {
      name: "Deliver and complete",
    });
    expect(deliver).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Mark as not applicable" }),
    );
    await user.type(
      screen.getByLabelText("Reason this requirement does not apply"),
      "No filing due",
    );
    await user.click(screen.getByRole("button", { name: "Save reason" }));
    expect(requests.post).toHaveBeenCalledWith(base + "/slots/filing/waive", {
      reason: "No filing due",
    });
    await waitFor(() => expect(deliver).toBeEnabled());
  });
  it("saves the agreed names for a custom service before presenting upload boxes", async () => {
    requests.get.mockResolvedValue({
      ...initial,
      bonus_slot: null,
      slots: [],
      needs_titles: true,
    });
    requests.post.mockResolvedValueOnce({
      ...initial,
      bonus_slot: null,
      slots: [slot("output_1", "Signed handover record")],
    });
    const { user } = setup();
    await user.type(
      await screen.findByLabelText(
        "Agreed final documents, one title per line",
      ),
      "Signed handover record",
    );
    await user.click(
      screen.getByRole("button", { name: "Save document titles" }),
    );
    expect(requests.post).toHaveBeenCalledWith(base + "/titles", {
      titles: ["Signed handover record"],
    });
    expect(
      await screen.findByLabelText("Upload Signed handover record"),
    ).toBeEnabled();
  });
  it("links a combined permit only when the server offers the eligible document", async () => {
    const first = slot("imk", "Renewed IMK", {
      document_id: 42,
      file_name: "Combined.pdf",
    });
    const second = slot("itas", "Renewed e-ITAS", { reusable_document_id: 42 });
    requests.get.mockResolvedValue({
      ...initial,
      bonus_slot: null,
      slots: [first, second],
    });
    requests.post.mockResolvedValueOnce({
      ...initial,
      bonus_slot: null,
      slots: [first, { ...second, document_id: 42, file_name: "Combined.pdf" }],
    });
    const { user } = setup();
    const button = await screen.findByRole("button", {
      name: "Confirm the existing permit covers both documents",
    });
    expect(
      screen.getByRole("button", { name: "Deliver and complete" }),
    ).toBeDisabled();
    await user.click(button);
    expect(requests.post).toHaveBeenCalledWith(base + "/slots/itas/reuse", {
      document_id: 42,
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Deliver and complete" }),
      ).toBeEnabled(),
    );
  });
  const followup = {
    slot_key: "itk",
    started_on: "2026-08-01",
    due_on: "2026-08-31",
    final_due_on: "2026-09-30",
    days_remaining: 0,
    status: "overdue",
    postponed_at: null,
    postponement_note: null,
    escalation_note: null,
    email_sent_at: null,
    fulfilled_at: null,
    owner_release_at: null,
    can_postpone: true,
    can_escalate: false,
    can_release: false,
  };
  it("records a private not-entered note once against the original 60-day deadline", async () => {
    requests.get.mockResolvedValue({ ...initial, visa_followup: followup });
    const note = "Client confirmed entry is still pending.";
    requests.post.mockResolvedValue({
      ...followup,
      status: "awaiting_permit",
      postponed_at: "2026-08-31T05:00:00Z",
      postponement_note: note,
      due_on: followup.final_due_on,
      days_remaining: 30,
      can_postpone: false,
    });
    const { user } = setup();
    expect(await screen.findByText(/0 days remaining/)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: /Client has not entered/ }),
    );
    expect(
      screen.getByRole("button", { name: /Confirm postponement/ }),
    ).toBeDisabled();
    await user.type(
      screen.getByLabelText("Private staff note about the client’s entry"),
      note,
    );
    await user.click(
      screen.getByRole("button", {
        name: /Confirm postponement to 2026-09-30/,
      }),
    );
    expect(requests.post).toHaveBeenCalledWith(
      base + "/visa-followup/postpone",
      { note },
    );
    expect(await screen.findByText(/30 days remaining/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /Client has not entered/ }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /email to Zero/i })).toBeNull();
  });
  it("submits an escalation only on deliberate confirmation, retries an explicit failure and shows the server receipt", async () => {
    const due = {
      ...followup,
      due_on: followup.final_due_on,
      can_postpone: false,
      can_escalate: true,
      status: "escalation_due",
    };
    const failed = { ...due, status: "email_failed" };
    requests.get
      .mockResolvedValueOnce({ ...initial, visa_followup: due })
      .mockResolvedValue({ ...initial, visa_followup: failed });
    requests.post
      .mockRejectedValueOnce(new Error("Mail not accepted"))
      .mockResolvedValueOnce({
        ...due,
        status: "awaiting_owner",
        email_sent_at: "2026-09-30T05:04:00Z",
        can_escalate: false,
      });
    const { user } = setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Prepare official email to Zero",
      }),
    );
    expect(screen.getByText(/Official email to Zero · case #12/)).toBeVisible();
    expect(requests.post).not.toHaveBeenCalled();
    await user.type(
      screen.getByLabelText("Note to Zero about the missing permit"),
      "Arrival evidence is outstanding.",
    );
    await user.click(
      screen.getByRole("button", { name: "Confirm and send email to Zero" }),
    );
    expect(await screen.findByText(/Email delivery failed/)).toBeVisible();
    expect(requests.post).toHaveBeenCalledTimes(1);
    await user.click(
      screen.getByRole("button", { name: "Confirm and send email to Zero" }),
    );
    expect(
      await screen.findByText(/Official email to Zero sent/),
    ).toBeVisible();
    expect(
      requests.post.mock.calls.filter(
        (c) => c[0] === base + "/visa-followup/escalate",
      ),
    ).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: /send email to Zero/i }),
    ).toBeNull();
  });
  it("never offers email retry where the provider outcome is uncertain", async () => {
    requests.get.mockResolvedValue({
      ...initial,
      visa_followup: {
        ...followup,
        status: "email_uncertain",
        can_postpone: false,
        can_escalate: false,
      },
    });
    setup();
    expect(await screen.findByText(/Email outcome unconfirmed/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /email to Zero/i })).toBeNull();
    expect(requests.post).not.toHaveBeenCalled();
  });
  it("withholds staff notes and actions if the response belongs to another credential", async () => {
    requests.sentSub = "staff-b";
    requests.get.mockResolvedValue({
      ...initial,
      visa_followup: {
        ...followup,
        postponement_note: "Internal example note",
      },
    });
    setup();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to load delivery requirements",
    );
    expect(screen.queryByText(/Internal example note/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Client has not entered/ }),
    ).toBeNull();
  });
  it("earns on the renewal IMK upload without requesting a visa issue date", async () => {
    const imk = slot("renewed_imk", "New IMK");
    const pack = { ...initial, bonus_slot: "renewed_imk", slots: [imk] };
    requests.get.mockResolvedValue(pack);
    requests.post.mockResolvedValueOnce({
      ...pack,
      bonus_event: "kitas_imk_uploaded",
      slots: [{ ...imk, document_id: 17, file_name: "imk.pdf" }],
    });
    const { user } = setup();
    const file = await screen.findByLabelText("Upload New IMK");
    expect(screen.queryByLabelText("Electronic Visa issue date")).toBeNull();
    await user.upload(
      file,
      new File(["permit"], "imk.pdf", { type: "application/pdf" }),
    );
    expect(
      await screen.findByText(/Bonus recorded on the renewed IMK upload/),
    ).toBeVisible();
    expect(requests.post.mock.calls[0][1]).not.toHaveProperty("issued_on");
  });
});

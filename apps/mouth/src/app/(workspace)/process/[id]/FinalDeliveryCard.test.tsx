import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Practice } from "@/lib/api/crm/crm.types";
import { ApiError } from "@/lib/api/error-handler";
import { FinalDeliveryCard } from "./FinalDeliveryCard";
import { workDayOf } from "@/lib/format/work-day";

const requests = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  companies: vi.fn(),
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
    crm: { getClientCompanies: requests.companies },
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

function setup(payment = "paid", canManageDelivery = true) {
  const complete = vi.fn();
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <FinalDeliveryCard
        practice={
          {
            id: 12,
            client_id: 19,
            payment_status: payment,
            can_manage_delivery: canManageDelivery,
          } as Practice
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
  it("describes an unconfigured service without offering a futile retry", async () => {
    requests.get.mockRejectedValue(
      new ApiError("No delivery requirements configured for this service", 409),
    );
    setup();
    expect(
      await screen.findByText(
        "No delivery checklist is configured for this service.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("completes a VOA extension on its first proof upload and shows a forfeited bonus", async () => {
    const permit = slot("document_1", "Renewed ITK — Visit Stay Permit");
    const pack = { ...initial, bonus_slot: null, slots: [permit] };
    requests.get.mockResolvedValue(pack);
    let finishUpload!: (value: unknown) => void;
    requests.post.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishUpload = resolve;
        }),
    );
    const { user, complete } = setup();
    expect(await screen.findByText("Renewed ITK")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Complete practice" }),
    ).toBeDisabled();
    expect(
      screen.getByText(
        "The first final document you upload completes the practice.",
      ),
    ).toBeVisible();
    expect(screen.getByText("PDF, JPG, PNG, DOCX · Max 3 MB")).toBeVisible();
    expect(requests.companies).not.toHaveBeenCalled();
    await user.upload(
      screen.getByLabelText("Upload Renewed ITK"),
      new File(["permit"], "renewed-itk.pdf", { type: "application/pdf" }),
    );
    expect(await screen.findByText("Saving…")).toBeVisible();
    finishUpload({
      ...pack,
      status: "completed",
      proof_ready: true,
      bonus_status: "forfeited",
      slots: [{ ...permit, document_id: 73, file_name: "renewed-itk.pdf" }],
    });
    expect(await screen.findByText("renewed-itk.pdf")).toBeVisible();
    expect(
      await screen.findByText("Document saved. Practice completed."),
    ).toBeVisible();
    expect(
      screen.getByText(
        "No bonus: the client paid less than the service price.",
      ),
    ).toBeVisible();
    await waitFor(() => expect(complete).toHaveBeenCalledOnce());
    expect(requests.post).toHaveBeenCalledTimes(1);
    expect(requests.post).toHaveBeenCalledWith(
      base + "/slots/document_1",
      expect.objectContaining({ file_name: "renewed-itk.pdf" }),
    );
  });

  it("keeps company filing optional and explains the upload blocker when company mode is selected", async () => {
    const pack = {
      ...initial,
      bonus_slot: null,
      slots: [slot("document_1", "Issued certificate")],
    };
    requests.get.mockResolvedValue(pack);
    requests.companies.mockResolvedValue([
      { company_id: 5, company_name: "Fixture Company" },
    ]);
    const { user } = setup();
    const file = await screen.findByLabelText("Upload Issued certificate");
    expect(file).toBeEnabled();
    await user.click(screen.getByText("Company filing (optional)"));
    await user.click(
      screen.getByRole("checkbox", {
        name: "Save uploads under a linked company",
      }),
    );
    expect(file).toBeDisabled();
    expect(
      screen.getByText(
        "Select the company in Company filing to enable upload.",
      ),
    ).toBeVisible();
    await screen.findByRole("option", { name: "Fixture Company" });
    await user.selectOptions(screen.getByRole("combobox"), "5");
    expect(file).toBeEnabled();
    expect(requests.post).not.toHaveBeenCalled();
  });

  it("explains view-only access without instructing a viewer to upload", async () => {
    requests.get.mockResolvedValue({
      ...initial,
      bonus_slot: null,
      slots: [slot("document_1", "Renewed ITK")],
    });
    setup("paid", false);
    expect(await screen.findByText("View-only access")).toBeVisible();
    expect(screen.getByText("Renewed ITK")).toBeVisible();
    expect(screen.getByText("Awaiting document")).toBeVisible();
    expect(screen.queryByLabelText("Upload Renewed ITK")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Complete practice" }),
    ).toBeNull();
  });

  it("names LKPM submission proof first and uploads against its original requirement key", async () => {
    const report = slot("document_1", "LKPM report · per commissioned period");
    const proof = slot(
      "document_2",
      "OSS filing acknowledgement · per commissioned period",
    );
    const pack = { ...initial, bonus_slot: null, slots: [report, proof] };
    requests.get.mockResolvedValue(pack);
    requests.post.mockResolvedValue({
      ...pack,
      slots: [
        report,
        { ...proof, document_id: 61, file_name: "lkpm-receipt.pdf" },
      ],
    });
    const { user } = setup();
    const file = await screen.findByLabelText("Upload LKPM submission proof");
    expect(
      screen
        .getAllByLabelText(/^Upload /)
        .map((el) => el.getAttribute("aria-label")),
    ).toEqual(["Upload LKPM submission proof", "Upload LKPM report"]);
    await user.upload(
      file,
      new File(["receipt"], "lkpm-receipt.pdf", { type: "application/pdf" }),
    );
    await screen.findByText("lkpm-receipt.pdf");
    expect(requests.post).toHaveBeenCalledWith(
      base + "/slots/document_2",
      expect.objectContaining({ file_name: "lkpm-receipt.pdf" }),
    );
    expect(
      screen.getByRole("button", { name: "Complete practice" }),
    ).toBeDisabled();
  });

  it("uses SPT for annual filing and preserves a specifically agreed custom document name", async () => {
    requests.get.mockResolvedValue({
      ...initial,
      bonus_slot: null,
      slots: [
        slot("document_1", "Filed SPT Tahunan"),
        slot("document_2", "Bukti Penerimaan Elektronik (BPE)"),
        slot("custom_1", "Lease — Signed amendment · Final"),
      ],
    });
    setup();
    expect(await screen.findByLabelText("Upload SPT")).toBeEnabled();
    expect(
      screen.getByLabelText("Upload BPE — SPT submission receipt"),
    ).toBeEnabled();
    expect(
      screen.getByLabelText("Upload Lease — Signed amendment · Final"),
    ).toBeEnabled();
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
      name: "Complete practice",
    });
    expect(deliver).toBeDisabled();
    const date = screen.getByLabelText("Electronic Visa issue date");
    expect(date).toHaveValue("");
    expect(screen.getByLabelText("Upload E-visa")).toBeDisabled();
    fireEvent.change(date, { target: { value: "2999-12-01" } });
    expect(screen.getByLabelText("Upload E-visa")).toBeDisabled();
    expect(requests.post).not.toHaveBeenCalled();
    fireEvent.change(date, { target: { value: printedDate } });
    await user.upload(
      screen.getByLabelText("Upload E-visa"),
      new File(["issued"], "visa.pdf", { type: "application/pdf" }),
    );
    await screen.findByText("Bonus recorded");
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
      screen.getByLabelText("Upload ITK"),
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
      screen.getByLabelText("Replace E-visa"),
      new File(["corrected"], "visa.pdf", { type: "application/pdf" }),
    );
    await waitFor(() => expect(requests.post).toHaveBeenCalledOnce());
    expect(requests.post.mock.calls[0][1]).not.toHaveProperty("issued_on");
  });

  it("cannot revise or deliver from an unsettled practice", async () => {
    setup("partial");
    await screen.findByText("E-visa");
    expect(screen.queryByLabelText("Upload E-visa")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Complete practice" }),
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
      name: "Complete practice",
    });
    expect(deliver).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Mark as not applicable" }),
    );
    const waiver = screen.getByLabelText(
      "Reason this requirement does not apply",
    );
    for (const invalid of ["ab", "x".repeat(501)]) {
      fireEvent.change(waiver, { target: { value: invalid } });
      expect(
        screen.getByRole("button", { name: "Save reason" }),
      ).toBeDisabled();
    }
    fireEvent.change(waiver, { target: { value: "x".repeat(500) } });
    expect(screen.getByRole("button", { name: "Save reason" })).toBeEnabled();
    await user.clear(waiver);
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
      screen.getByRole("button", { name: "Complete practice" }),
    ).toBeDisabled();
    await user.click(button);
    expect(requests.post).toHaveBeenCalledWith(base + "/slots/itas/reuse", {
      document_id: 42,
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Complete practice" }),
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
    can_postpone: true,
    can_escalate: false,
  };
  it("adds a later document to a completed practice only where the server allows it", async () => {
    const closed = {
      ...initial,
      status: "completed",
      bonus_event: "electronic_visa_issued",
      bonus_rate: 0,
      slots: [
        slot("electronic_visa", "Electronic Visa", { document_id: 71 }),
        slot("itk", "ITK", { can_repair: true }),
      ],
    };
    requests.get.mockResolvedValue(closed);
    requests.post.mockResolvedValueOnce({
      ...closed,
      slots: [closed.slots[0], slot("itk", "ITK", { document_id: 72 })],
    });
    const { user, complete } = setup();
    const input = await screen.findByLabelText("Upload ITK");
    expect(screen.getByText("This service carries no bonus.")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Complete practice" }),
    ).toBeNull();
    expect(screen.queryByLabelText("Replace E-visa")).toBeNull();
    await user.upload(
      input,
      new File(["%PDF-1.7"], "ITK.pdf", { type: "application/pdf" }),
    );
    await waitFor(() =>
      expect(requests.post).toHaveBeenCalledWith(
        base + "/slots/itk/repair",
        expect.objectContaining({ file_name: "ITK.pdf" }),
      ),
    );
    expect(await screen.findByText("Document saved.")).toBeVisible();
    expect(complete).not.toHaveBeenCalled();
  });

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
    const failed = {
      ...due,
      status: "email_failed",
      escalation_note: "Arrival evidence is outstanding.",
    };
    requests.get
      .mockResolvedValueOnce({ ...initial, visa_followup: due })
      .mockResolvedValue({ ...initial, visa_followup: failed });
    requests.post
      .mockRejectedValueOnce(new Error("Mail not accepted"))
      .mockResolvedValueOnce({
        ...due,
        status: "email_sent_permit_due",
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
    const reason = screen.getByLabelText(
      "Note to Zero about the missing permit",
    );
    const send = screen.getByRole("button", {
      name: "Confirm and send email to Zero",
    });
    for (const invalid of ["ab", "x".repeat(2001)]) {
      fireEvent.change(reason, { target: { value: invalid } });
      expect(send).toBeDisabled();
    }
    fireEvent.change(reason, { target: { value: "x".repeat(2000) } });
    expect(send).toBeEnabled();
    await user.clear(reason);
    await user.type(
      screen.getByLabelText("Note to Zero about the missing permit"),
      "Arrival evidence is outstanding.",
    );
    await user.click(
      screen.getByRole("button", { name: "Confirm and send email to Zero" }),
    );
    expect(await screen.findByText(/Email delivery failed/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(
      screen.getByRole("button", { name: "Review and retry email to Zero" }),
    );
    const original = screen.getByLabelText(
      "Note to Zero about the missing permit",
    );
    expect(original).toHaveValue("Arrival evidence is outstanding.");
    expect(original).toHaveAttribute("readonly");
    expect(screen.getByText(/Recorded reason to Zero/)).toHaveTextContent(
      "Arrival evidence is outstanding.",
    );
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
    expect(
      screen.queryByRole("button", {
        name: /Record receipt of the existing email/,
      }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /email to Zero/i })).toBeNull();
    expect(requests.post).not.toHaveBeenCalled();
  });
  it("lets the authorised owner confirm an existing ambiguous send by reference without sending or releasing", async () => {
    const uncertain = {
      ...followup,
      status: "email_uncertain",
      can_postpone: false,
      can_escalate: false,
      can_confirm_receipt: true,
    };
    requests.get.mockResolvedValue({ ...initial, visa_followup: uncertain });
    requests.post.mockResolvedValueOnce({
      ...uncertain,
      status: "email_sent_permit_due",
      email_sent_at: "2026-09-30T05:00:00Z",
      email_receipt_confirmed_at: "2026-09-30T05:06:00Z",
      can_confirm_receipt: false,
    });
    const { user } = setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Record receipt of the existing email",
      }),
    );
    const confirm = screen.getByRole("button", {
      name: "Confirm existing email receipt",
    });
    expect(confirm).toBeDisabled();
    await user.type(
      screen.getByLabelText(
        "Mailbox Message-ID or Brevo reference for the received email",
      ),
      "  receipt-ref-42  ",
    );
    expect(requests.post).not.toHaveBeenCalled();
    await user.click(confirm);
    expect(
      await screen.findByText("Email receipt confirmed by Zero."),
    ).toBeVisible();
    expect(requests.post.mock.calls).toEqual([
      [
        base + "/visa-followup/confirm-receipt",
        { reference: "receipt-ref-42" },
      ],
    ]);
    expect(
      screen.queryByRole("button", {
        name: "Record receipt of the existing email",
      }),
    ).toBeNull();
    expect(
      screen.getByText(
        "The bonus payment stays on hold until the required permit is uploaded.",
      ),
    ).toBeVisible();
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
    expect(await screen.findByText(/Bonus recorded/)).toBeVisible();
    expect(requests.post.mock.calls[0][1]).not.toHaveProperty("issued_on");
  });

  describe("after a lost upload response", () => {
    const permit = slot("document_1", "Renewed ITK — Visit Stay Permit");
    const open = { ...initial, bonus_slot: null, slots: [permit] };
    async function failUpload(refreshed: unknown) {
      requests.get.mockResolvedValueOnce(open).mockResolvedValue(refreshed);
      requests.post.mockRejectedValueOnce(new Error("Connection lost"));
      const view = setup();
      await view.user.upload(
        await screen.findByLabelText("Upload Renewed ITK"),
        new File(["permit"], "renewed-itk.pdf", { type: "application/pdf" }),
      );
      return view;
    }

    it("claims completion only when that slot now holds a new document", async () => {
      const { complete } = await failUpload({
        ...open,
        status: "completed",
        slots: [{ ...permit, document_id: 73, file_name: "renewed-itk.pdf" }],
      });
      expect(
        await screen.findByText(
          "Final documents delivered. Practice completed.",
        ),
      ).toBeVisible();
      expect(screen.queryByText("Connection lost")).toBeNull();
      await waitFor(() => expect(complete).toHaveBeenCalledOnce());
    });

    it("keeps the failure visible when another upload completed the practice and this slot is still empty", async () => {
      const { complete } = await failUpload({ ...open, status: "completed" });
      expect(await screen.findByText("Connection lost")).toBeVisible();
      expect(
        screen.queryByText("Final documents delivered. Practice completed."),
      ).toBeNull();
      await waitFor(() => expect(complete).toHaveBeenCalledOnce());
    });
  });

  it("offers the titles form on a completed custom practice that has none yet", async () => {
    requests.get.mockResolvedValue({
      ...initial,
      status: "completed",
      bonus_slot: null,
      slots: [],
      needs_titles: true,
    });
    requests.post.mockResolvedValueOnce({
      ...initial,
      status: "completed",
      bonus_slot: null,
      slots: [slot("custom_1", "Signed handover record", { can_repair: true })],
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
  });

  it("hides the titles form from a completed practice the viewer cannot manage", async () => {
    requests.get.mockResolvedValue({
      ...initial,
      status: "completed",
      bonus_slot: null,
      slots: [],
      needs_titles: true,
    });
    setup("paid", false);
    await screen.findByText("View-only access");
    expect(
      screen.queryByLabelText("Agreed final documents, one title per line"),
    ).toBeNull();
  });
});

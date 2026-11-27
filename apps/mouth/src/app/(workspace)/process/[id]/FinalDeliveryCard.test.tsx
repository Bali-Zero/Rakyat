import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
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

  it("guides a VOA extension from the named missing permit to a saved document and completion", async () => {
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
    const button = screen.getByRole("button", {
      name: "Complete practice",
    });
    expect(button).toBeDisabled();
    expect(screen.getByText("1 document remaining")).toBeVisible();
    expect(screen.getByText("PDF, JPG, PNG, DOCX · Max 3 MB")).toBeVisible();
    expect(requests.companies).not.toHaveBeenCalled();
    await user.upload(
      screen.getByLabelText("Upload Renewed ITK"),
      new File(["permit"], "renewed-itk.pdf", { type: "application/pdf" }),
    );
    expect(await screen.findByText("Saving…")).toBeVisible();
    expect(button).toBeDisabled();
    const ready = {
      ...pack,
      slots: [{ ...permit, document_id: 73, file_name: "renewed-itk.pdf" }],
    };
    finishUpload(ready);
    expect(await screen.findByText("renewed-itk.pdf")).toBeVisible();
    expect(await screen.findByText("Ready to complete")).toBeVisible();
    expect(button).toBeEnabled();
    expect(requests.post).toHaveBeenCalledTimes(1);
    expect(requests.post).toHaveBeenCalledWith(
      base + "/slots/document_1",
      expect.objectContaining({ file_name: "renewed-itk.pdf" }),
    );
    requests.post.mockResolvedValueOnce({ ...ready, status: "completed" });
    await user.click(button);
    await waitFor(() => expect(complete).toHaveBeenCalledOnce());
    expect(requests.post).toHaveBeenLastCalledWith(base + "/complete");
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
  it("restores only a server-authorised missing permit on a completed practice", async () => {
    const closed = {
      ...initial,
      status: "completed",
      bonus_event: "electronic_visa_issued",
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
    const input = await screen.findByLabelText("Restore ITK");
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
    expect(await screen.findByText("Missing permit restored.")).toBeVisible();
    expect(complete).not.toHaveBeenCalled();
  });

  describe("after a response is lost the card claims only what it can prove", () => {
    const sha256 = async (text: string) =>
      Array.from(
        new Uint8Array(
          await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)),
        ),
        (byte) => byte.toString(16).padStart(2, "0"),
      ).join("");
    const open = {
      ...initial,
      bonus_event: "electronic_visa_issued",
      slots: [
        slot("electronic_visa", "Electronic Visa", { document_id: 21 }),
        slot("itk", "ITK — Visit Stay Permit"),
      ],
    };
    const withItk = (extras: Record<string, unknown>) => ({
      ...open,
      slots: [open.slots[0], slot("itk", "ITK — Visit Stay Permit", extras)],
    });
    const upload = async (user: ReturnType<typeof setup>["user"]) =>
      user.upload(
        await screen.findByLabelText("Upload ITK"),
        new File(["bytes of A"], "ITK.pdf", { type: "application/pdf" }),
      );

    it("accepts the refreshed slot only when it holds the very file that was sent", async () => {
      requests.get.mockResolvedValueOnce(open).mockResolvedValue(
        withItk({
          document_id: 40,
          content_sha256: await sha256("bytes of A"),
        }),
      );
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      const { user } = setup();
      await upload(user);
      expect(await screen.findByText("Document saved.")).toBeVisible();
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("keeps the error and names the conflict when the slot now holds other bytes under the same name", async () => {
      requests.get.mockResolvedValueOnce(open).mockResolvedValue(
        withItk({
          document_id: 41,
          file_name: "ITK.pdf",
          content_sha256: await sha256("bytes of B"),
        }),
      );
      requests.post.mockRejectedValueOnce(
        new ApiError(
          "Another operator changed this slot. Refresh and review.",
          409,
        ),
      );
      const { user } = setup();
      await upload(user);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Another operator changed this slot.*Another file is now in this slot/,
      );
      expect(screen.queryByText("Document saved.")).toBeNull();
    });

    it("keeps the error when the saved document carries no digest", async () => {
      requests.get
        .mockResolvedValueOnce(open)
        .mockResolvedValue(withItk({ document_id: 42, content_sha256: null }));
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      const { user } = setup();
      await upload(user);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "network lost",
      );
      expect(screen.queryByText("Document saved.")).toBeNull();
    });

    it("does not call a failed Complete a success unless the practice is completed", async () => {
      const ready = withItk({ document_id: 43 });
      requests.get.mockResolvedValue(ready);
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      const { user, complete } = setup();
      await user.click(
        await screen.findByRole("button", { name: "Complete practice" }),
      );
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "network lost",
      );
      expect(complete).not.toHaveBeenCalled();
      requests.get.mockResolvedValue({ ...ready, status: "completed" });
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      await user.click(
        await screen.findByRole("button", { name: "Complete practice" }),
      );
      await waitFor(() => expect(complete).toHaveBeenCalledOnce());
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("counts a waiver or a combined-permit link as saved only when that slot shows it", async () => {
      const conditional = slot("filing", "Filing receipt", {
        condition: "If filing is due",
      });
      const waivable = { ...initial, bonus_slot: null, slots: [conditional] };
      const waivedNow = {
        ...waivable,
        slots: [{ ...conditional, waiver_reason: "No filing due" }],
      };
      const waive = async (user: ReturnType<typeof setup>["user"]) => {
        await user.click(
          await screen.findByRole("button", { name: "Mark as not applicable" }),
        );
        await user.type(
          screen.getByLabelText("Reason this requirement does not apply"),
          "No filing due",
        );
        await user.click(screen.getByRole("button", { name: "Save reason" }));
      };
      requests.get.mockResolvedValueOnce(waivable).mockResolvedValue(waivable);
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      let { user } = setup();
      await waive(user);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "network lost",
      );
      cleanup();
      requests.get.mockResolvedValueOnce(waivable).mockResolvedValue(waivedNow);
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      ({ user } = setup());
      await waive(user);
      expect(await screen.findByText("Requirement updated.")).toBeVisible();
      expect(screen.queryByRole("alert")).toBeNull();
      cleanup();
      const first = slot("imk", "Renewed IMK", { document_id: 42 });
      const second = slot("itas", "Renewed e-ITAS", {
        reusable_document_id: 42,
      });
      const linkable = { ...initial, bonus_slot: null, slots: [first, second] };
      const link = async (u: ReturnType<typeof setup>["user"]) =>
        u.click(
          await screen.findByRole("button", {
            name: "Confirm the existing permit covers both documents",
          }),
        );
      requests.get.mockResolvedValueOnce(linkable).mockResolvedValue(linkable);
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      ({ user } = setup());
      await link(user);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "network lost",
      );
      cleanup();
      requests.get.mockResolvedValueOnce(linkable).mockResolvedValue({
        ...linkable,
        slots: [first, { ...second, document_id: 42 }],
      });
      requests.post.mockRejectedValueOnce(new Error("network lost"));
      ({ user } = setup());
      await link(user);
      expect(
        await screen.findByText("Combined permit linked to both requirements."),
      ).toBeVisible();
    });

    it("tells the operator the practice is completed when the upload was the one that completed it", async () => {
      requests.get.mockResolvedValue(open);
      requests.post.mockResolvedValueOnce({
        ...withItk({ document_id: 44 }),
        status: "completed",
      });
      const { user, complete } = setup();
      await upload(user);
      expect(
        await screen.findByText("Document saved. The practice is completed."),
      ).toBeVisible();
      expect(complete).toHaveBeenCalledOnce();
    });
  });

  it("offers Upload, not Restore, on a delivered practice and says the document was added", async () => {
    const delivered = {
      ...initial,
      status: "completed",
      delivery_recorded: true,
      bonus_event: "electronic_visa_issued",
      slots: [
        slot("electronic_visa", "Electronic Visa", { document_id: 71 }),
        slot("itk", "ITK", { can_repair: true }),
      ],
    };
    requests.get.mockResolvedValue(delivered);
    requests.post.mockResolvedValueOnce({
      ...delivered,
      slots: [delivered.slots[0], slot("itk", "ITK", { document_id: 72 })],
    });
    const { user } = setup();
    await user.upload(
      await screen.findByLabelText("Upload ITK"),
      new File(["%PDF-1.7"], "ITK.pdf", { type: "application/pdf" }),
    );
    expect(await screen.findByText("Document added.")).toBeVisible();
    expect(screen.queryByLabelText("Restore ITK")).toBeNull();
  });

  describe("a November practice completed before Final delivery", () => {
    const legacy = (extras: Record<string, unknown> = {}) => ({
      ...initial,
      status: "completed",
      legacy_proof: true,
      bonus_instruction: "upload_or_waive",
      slots: [
        slot("electronic_visa", "Electronic Visa", {
          condition: "if issued online",
          can_repair: true,
        }),
        slot("itk", "ITK — Visit Stay Permit", { can_repair: true }),
      ],
      ...extras,
    });
    it("offers every empty document, asks for the issue date and tells how the bonus is recorded", async () => {
      requests.get.mockResolvedValue(legacy());
      requests.post.mockResolvedValue(legacy());
      const { user, complete } = setup();
      expect(
        await screen.findByText(
          "Upload the E-visa, or mark it not applicable before uploading another document, to record the bonus.",
        ),
      ).toBeVisible();
      expect(screen.getByLabelText("Upload ITK")).toBeEnabled();
      expect(
        screen.queryByRole("button", { name: "Complete practice" }),
      ).toBeNull();
      expect(
        screen.getByRole("button", { name: "Mark as not applicable" }),
      ).toBeVisible();
      const visa = screen.getByLabelText("Upload E-visa");
      expect(visa).toBeDisabled();
      fireEvent.change(screen.getByLabelText("Electronic Visa issue date"), {
        target: { value: workDayOf() },
      });
      await user.upload(
        visa,
        new File(["%PDF-1.7"], "evisa.pdf", { type: "application/pdf" }),
      );
      await waitFor(() =>
        expect(requests.post).toHaveBeenCalledWith(
          base + "/slots/electronic_visa/repair",
          expect.objectContaining({
            file_name: "evisa.pdf",
            issued_on: workDayOf(),
          }),
        ),
      );
      expect(complete).not.toHaveBeenCalled();
    });
    it("saves the document titles of a cohort custom practice", async () => {
      const unnamed = legacy({
        needs_titles: true,
        bonus_slot: null,
        bonus_instruction: null,
        slots: [],
      });
      requests.get.mockResolvedValue(unnamed);
      requests.post.mockResolvedValue(
        legacy({
          bonus_slot: null,
          bonus_instruction: null,
          slots: [
            slot("custom_1", "Deed", { can_repair: true }),
            slot("custom_2", "Receipt", { can_repair: true }),
          ],
        }),
      );
      const { user } = setup();
      await user.type(
        await screen.findByLabelText(
          "Agreed final documents, one title per line",
        ),
        "Deed{Enter}Receipt",
      );
      await user.click(
        screen.getByRole("button", { name: "Save document titles" }),
      );
      await waitFor(() =>
        expect(requests.post).toHaveBeenCalledWith(base + "/titles", {
          titles: ["Deed", "Receipt"],
        }),
      );
      expect(await screen.findByText("Document titles saved.")).toBeVisible();
      expect(screen.getByLabelText("Upload Deed")).toBeEnabled();
      expect(screen.getByLabelText("Upload Receipt")).toBeEnabled();
    });
    it("saves the waiver of a cohort conditional document", async () => {
      requests.get.mockResolvedValue(legacy());
      requests.post.mockResolvedValue(
        legacy({
          bonus_instruction: null,
          slots: [
            slot("electronic_visa", "Electronic Visa", {
              condition: "if issued online",
              waiver_reason: "Issued at the border",
            }),
            slot("itk", "ITK — Visit Stay Permit", { can_repair: true }),
          ],
        }),
      );
      const { user } = setup();
      await user.click(
        await screen.findByRole("button", { name: "Mark as not applicable" }),
      );
      await user.type(
        screen.getByLabelText("Reason this requirement does not apply"),
        "Issued at the border",
      );
      await user.click(screen.getByRole("button", { name: "Save reason" }));
      await waitFor(() =>
        expect(requests.post).toHaveBeenCalledWith(
          base + "/slots/electronic_visa/waive",
          { reason: "Issued at the border" },
        ),
      );
      expect(await screen.findByText("Requirement updated.")).toBeVisible();
      expect(await screen.findByText("Not applicable")).toBeVisible();
      expect(screen.queryByText(/to record the bonus/)).toBeNull();
    });
    it.each([
      {
        day: 30,
        extras: { can_postpone: true, status: "overdue" },
        open: "Client has not entered · postpone once",
        label: "Private staff note about the client’s entry",
        send: /Confirm postponement/,
        action: "postpone",
        done: "Postponement recorded.",
      },
      {
        day: 60,
        extras: {
          can_postpone: false,
          can_escalate: true,
          status: "escalation_due",
        },
        open: "Prepare official email to Zero",
        label: "Note to Zero about the missing permit",
        send: "Confirm and send email to Zero",
        action: "escalate",
        done: "Escalation status updated.",
      },
    ])(
      "manages the permit follow-up of a cohort practice at day $day",
      async ({ extras, open, label, send, action, done }) => {
        const tracked = { ...followup, ...extras };
        requests.get.mockResolvedValue(
          legacy({
            bonus_instruction: null,
            visa_followup: tracked,
            slots: [
              slot("electronic_visa", "Electronic Visa", { document_id: 71 }),
              slot("itk", "ITK — Visit Stay Permit", { can_repair: true }),
            ],
          }),
        );
        requests.post.mockResolvedValue({
          ...tracked,
          status: "awaiting_permit",
        });
        const { user } = setup();
        await user.click(await screen.findByRole("button", { name: open }));
        await user.type(screen.getByLabelText(label), "Client is abroad.");
        await user.click(screen.getByRole("button", { name: send }));
        await waitFor(() =>
          expect(requests.post).toHaveBeenCalledWith(
            `${base}/visa-followup/${action}`,
            { note: "Client is abroad." },
          ),
        );
        expect(await screen.findByText(done)).toBeVisible();
      },
    );
    it("says the bonus document is required once the waiver can no longer help, and says nothing otherwise", async () => {
      requests.get.mockResolvedValue(legacy({ bonus_instruction: "upload" }));
      setup();
      expect(
        await screen.findByText("The E-visa is required to record the bonus."),
      ).toBeVisible();
      cleanup();
      requests.get.mockResolvedValue(legacy({ bonus_instruction: null }));
      setup();
      await screen.findByLabelText("Upload ITK");
      expect(screen.queryByText(/to record the bonus/)).toBeNull();
      expect(screen.queryByText(/is required to record/)).toBeNull();
    });
    it("shows no upload on a completed practice outside the cohort or to a view-only user", async () => {
      requests.get.mockResolvedValue(
        legacy({
          legacy_proof: false,
          bonus_instruction: null,
          slots: [slot("electronic_visa", "Electronic Visa")],
        }),
      );
      setup();
      await screen.findByText("E-visa");
      expect(screen.queryByLabelText("Upload E-visa")).toBeNull();
      cleanup();
      requests.get.mockResolvedValue(legacy());
      setup("paid", false);
      await screen.findByText("View-only access");
      expect(screen.queryByLabelText("Upload ITK")).toBeNull();
    });
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

  it("tells the employee when a service carries no bonus or the bonus was forfeited", async () => {
    const imk = slot("renewed_imk", "New IMK", { document_id: 17 });
    const custom = { ...initial, bonus_slot: null, bonus_rate: 0, slots: [] };
    requests.get.mockResolvedValue(custom);
    setup();
    expect(
      await screen.findByText("This service carries no bonus."),
    ).toBeVisible();
    cleanup();

    const earned = {
      ...initial,
      status: "completed",
      bonus_slot: "renewed_imk",
      bonus_event: "kitas_imk_uploaded",
      bonus_status: "no_bonus",
      slots: [imk],
    };
    requests.get.mockResolvedValue(earned);
    setup();
    expect(
      await screen.findByText("This service carries no bonus."),
    ).toBeVisible();
    expect(screen.queryByText("Bonus recorded")).toBeNull();
    cleanup();

    requests.get.mockResolvedValue({
      ...earned,
      bonus_rate: 15000,
      bonus_status: "forfeited",
    });
    setup();
    expect(
      await screen.findByText(
        "No bonus: the client paid less than the service price.",
      ),
    ).toBeVisible();
    expect(screen.queryByText("Bonus recorded")).toBeNull();
    expect(screen.queryByText("This service carries no bonus.")).toBeNull();
  });
});

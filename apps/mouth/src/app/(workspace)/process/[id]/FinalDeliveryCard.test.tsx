import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Practice } from "@/lib/api/crm/crm.types";
import { FinalDeliveryCard } from "./FinalDeliveryCard";

const requests = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: requests }));

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
    requests.get.mockResolvedValue(initial);
  });
  it("records issuance on upload and keeps completion unavailable until the post-entry permit is saved", async () => {
    const issuedVisa = slot("electronic_visa", "Electronic Visa", {
      document_id: 21,
      file_name: "visa.pdf",
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
    await user.upload(
      screen.getByLabelText("Upload Electronic Visa"),
      new File(["issued"], "visa.pdf", { type: "application/pdf" }),
    );
    await screen.findByText(
      "Electronic Visa issuance recorded. Remaining documents do not earn another bonus.",
    );
    expect(requests.post).toHaveBeenCalledWith(
      base + "/slots/electronic_visa",
      {
        file_name: "visa.pdf",
        file_content: "aXNzdWVk",
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
});

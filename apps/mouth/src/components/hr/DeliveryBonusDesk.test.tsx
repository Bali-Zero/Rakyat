import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DeliveryBonusDesk } from "./DeliveryBonusDesk";
const mocks = vi.hoisted(() => ({
  owner: { sub: "employee-a", epoch: 1 },
  request: vi.fn(),
}));
vi.mock("@/lib/auth/session-identity", () => ({
  useSessionIdentity: () => mocks.owner,
  isSameIdentity: (a: typeof mocks.owner, b: typeof mocks.owner) =>
    a?.sub === b?.sub && a?.epoch === b?.epoch,
  acceptResponse: (
    res: { sentSub: string },
    identity: typeof mocks.owner,
    owner?: string,
  ) => res.sentSub === identity.sub && (!owner || owner === identity.sub),
}));
vi.mock("@/lib/api", () => ({ api: { requestWithCredential: mocks.request } }));
const desk = () => ({
  team_member_id: "employee-a",
  on_payroll: true,
  month: "2026-11-01",
  milestones: 3,
  target: 5,
  totals: {
    pending: 100000,
    approved: 0,
    paid: 0,
    awaiting_rate: 1,
    payroll_locked: 0,
  },
  recent: [
    {
      practice_id: 47,
      service: "C1 Tourism",
      event_kind: "electronic_visa_issued",
      earned_at: "2026-11-29T10:00:00Z",
      awarded_at: "2026-11-30T16:00:00Z",
      status: "pending",
      amount_idr: 100000,
    },
  ],
  next_case: {
    practice_id: 47,
    service: "C1 Tourism",
    amount_idr: 100000,
    already_earned: true,
  },
});
describe("DeliveryBonusDesk", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.owner.sub = "employee-a";
  });
  it("shows the issuance, outstanding ITK, payroll month and an independently editable goal", async () => {
    let d = desk();
    mocks.request.mockImplementation(
      async (url: string, opts?: { body?: string }) => {
        if (url.endsWith("/delivery-goal"))
          d = { ...d, target: JSON.parse(opts!.body!).target };
        return {
          sentSub: "employee-a",
          data: url.endsWith("/delivery")
            ? {
                slots: [
                  { label: "Electronic Visa", document_id: 8 },
                  { label: "ITK", document_id: null },
                ],
              }
            : d,
        };
      },
    );
    render(<DeliveryBonusDesk />);
    expect(
      await screen.findByRole("link", { name: "C1 Tourism · ITK" }),
    ).toHaveAttribute("href", "/process/47#final-delivery");
    expect(screen.getByText("Bonus milestone already recorded")).toBeVisible();
    expect(
      screen.getByText(/Electronic Visa uploaded.*Dec 2026 payroll/),
    ).toBeVisible();
    expect(screen.getByText("1 awaiting a rate")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Edit personal goal" }));
    fireEvent.change(screen.getByRole("spinbutton"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("4 more to reach your goal")).toBeVisible();
  });
  it("drops a response owned by another employee without rendering payroll figures", async () => {
    mocks.request.mockResolvedValue({ sentSub: "employee-b", data: desk() });
    render(<DeliveryBonusDesk />);
    await waitFor(() => expect(mocks.request).toHaveBeenCalledOnce());
    expect(
      screen.queryByRole("heading", { name: "My delivery bonuses" }),
    ).toBeNull();
  });
  it.each([
    ["not_sent", "send_escalation_email", /Review official email to Zero/],
    [
      "sent",
      "upload_after_email",
      /Upload the required permit to clear this hold/,
    ],
  ] as const)(
    "shows the held payout and the next document action for email state %s",
    async (emailState, nextAction, expectedAction) => {
      const old = desk();
      const value = {
        ...old,
        holds: [
          {
            practice_id: 47,
            service: "C1 Tourism",
            slot_key: "document_2",
            slot_label: "ITK — Visit Stay Permit · after confirmed entry",
            due_on: "2026-11-28",
            payout_from_month: "2026-12-01",
            next_action: nextAction,
            email_state: emailState,
          },
        ],
        recent: [
          {
            ...old.recent[0],
            event_kind: "kitas_imk_uploaded",
            service: "KITAS Renewal",
          },
        ],
        next_case: null,
      };
      mocks.request.mockResolvedValue({ sentSub: "employee-a", data: value });
      render(<DeliveryBonusDesk />);
      expect(
        await screen.findByRole("link", { name: "Case #47 · C1 Tourism" }),
      ).toHaveAttribute("href", "/process/47#final-delivery");
      expect(screen.getByText(expectedAction)).toBeVisible();
      expect(screen.getByText(/ITK — Visit Stay Permit/)).toHaveTextContent(
        "Due 28 Nov 2026",
      );
      expect(screen.queryByText(/document_2/i)).toBeNull();
      expect(screen.getByText("28 Nov 2026")).toHaveAttribute(
        "datetime",
        "2026-11-28",
      );
      expect(
        screen.getByText("Earliest payroll month · December 2026"),
      ).toBeVisible();
      expect(screen.getByText(/Renewed IMK uploaded/)).toBeVisible();
      expect(screen.queryByRole("button", { name: /send/i })).toBeNull();
    },
  );
  it("keeps unpriced and held cases out of the payroll amount and separates the goal month", async () => {
    const sample = desk();
    const data = {
      ...sample,
      next_case: null,
      totals: {
        pending: 100000,
        approved: 240000,
        paid: 68000,
        awaiting_rate: 1,
        payroll_locked: 2,
      },
    };
    mocks.request.mockResolvedValue({ sentSub: "employee-a", data });
    render(<DeliveryBonusDesk />);
    expect(await screen.findByText("Rp 408.000")).toBeVisible();
    expect(screen.getByText("2 awaiting payroll review")).toBeVisible();
    expect(
      screen.getByRole("progressbar", { name: "Monthly service goal" }),
    ).toHaveAttribute("aria-valuetext", "3 qualifying services, goal 5");
    expect(screen.getByText(/Dec 2026 payroll/)).toBeVisible();
  });
  it("inspects a payroll category using only priced amounts from the payroll period", async () => {
    const data = {
      ...desk(),
      next_case: null,
      totals: {
        pending: 150000,
        approved: 100000,
        paid: 0,
        awaiting_rate: 5,
        payroll_locked: 1,
      },
    };
    mocks.request.mockResolvedValue({ sentSub: "employee-a", data });
    render(<DeliveryBonusDesk />);
    const approved = await screen.findByRole("button", {
      name: /Approved.*Rp 100.000/,
    });
    fireEvent.click(approved);
    expect(approved).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Approved · 40% of this payroll total",
    );
    fireEvent.click(approved);
    expect(approved).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("status")).toBeNull();
    expect(mocks.request).toHaveBeenCalledOnce();
  });

  it("filters the available recent records by status and WITA award month, then restores all activity", async () => {
    const sample = desk(),
      common = sample.recent[0];
    const recent = [
      {
        ...common,
        practice_id: 61,
        service: "November approved",
        status: "approved",
        earned_at: "2026-10-29T10:00:00Z",
        awarded_at: "2026-10-31T16:01:00Z",
      },
      {
        ...common,
        practice_id: 62,
        service: "October approved",
        status: "approved",
        awarded_at: "2026-10-31T15:59:00Z",
      },
      {
        ...common,
        practice_id: 63,
        service: "November pending",
        status: "pending",
        awarded_at: "2026-11-10T12:00:00Z",
      },
    ];
    mocks.request.mockResolvedValue({
      sentSub: "employee-a",
      data: { ...sample, recent, next_case: null },
    });
    render(<DeliveryBonusDesk />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Approved.*Rp/ }),
    );
    expect(
      screen.getByRole("link", { name: "November approved" }),
    ).toHaveAttribute("href", "/process/61");
    expect(screen.queryByText("October approved")).toBeNull();
    expect(screen.queryByText("November pending")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Paid.*Rp/ }));
    expect(
      screen.getByText("No matching deliveries in recent activity."),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    fireEvent.click(screen.getByText("View 2 earlier deliveries"));
    expect(
      screen.getByRole("link", { name: "October approved" }),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "November pending" }),
    ).toBeVisible();
    expect(mocks.request).toHaveBeenCalledOnce();
  });

  it("counts fulfilled, company-filed and waived requirements from the actual delivery slots", async () => {
    mocks.request.mockImplementation(async (path: string) => ({
      sentSub: "employee-a",
      data: path.endsWith("/delivery")
        ? {
            slots: [
              { key: "visa", label: "Electronic Visa", document_id: 8 },
              {
                key: "company",
                label: "Company file",
                document_id: null,
                company_document_id: 9,
              },
              {
                key: "conditional",
                label: "Conditional letter",
                document_id: null,
                waiver_reason: "Not applicable",
              },
              { key: "permit", label: "ITK", document_id: null },
            ],
          }
        : desk(),
    }));
    render(<DeliveryBonusDesk />);
    await screen.findByRole("link", { name: "C1 Tourism · ITK" });
    expect(
      screen.getByText("Requirements met").parentElement,
    ).toHaveTextContent("3 / 4");
    expect(mocks.request).toHaveBeenCalledTimes(2);
  });

  it("leaves an empty delivery pack open for review and shows actual over-target progress", async () => {
    const data = { ...desk(), milestones: 6, target: 4 };
    mocks.request.mockImplementation(async (path: string) => ({
      sentSub: "employee-a",
      data: path.endsWith("/delivery") ? { slots: [] } : data,
    }));
    render(<DeliveryBonusDesk />);
    const next = await screen.findByRole("region", {
      name: "Next paid practice",
    });
    await waitFor(() => expect(mocks.request).toHaveBeenCalledTimes(2));
    expect(next).not.toHaveTextContent("Confirm delivery");
    expect(next).not.toHaveTextContent("Requirements met");
    expect(screen.getByText("150%")).toBeVisible();
    expect(
      screen.getByRole("progressbar", { name: "Monthly service goal" }),
    ).toHaveAttribute("aria-valuenow", "4");
  });

  it("opens earlier activity without losing the latest delivery or changing payroll", async () => {
    const sample = desk();
    const earlier = {
      ...sample.recent[0],
      practice_id: 48,
      service: "Synthetic filing",
      amount_idr: null,
      status: "awaiting_rate",
    };
    mocks.request.mockResolvedValue({
      sentSub: "employee-a",
      data: { ...sample, recent: [sample.recent[0], earlier], next_case: null },
    });
    render(<DeliveryBonusDesk />);
    expect(
      await screen.findByRole("link", { name: "C1 Tourism" }),
    ).toBeVisible();
    expect(screen.getByText("Synthetic filing")).not.toBeVisible();
    fireEvent.click(screen.getByText("View 1 earlier delivery"));
    expect(
      screen.getByRole("link", { name: "Synthetic filing" }),
    ).toHaveAttribute("href", "/process/48");
    expect(screen.getByText("Rate to confirm")).toBeVisible();
    fireEvent.click(screen.getByText("View 1 earlier delivery"));
    expect(screen.getByText("Synthetic filing")).not.toBeVisible();
    expect(mocks.request).toHaveBeenCalledTimes(1);
  });
  it("names a forfeited and a no-bonus delivery and a next case that will forfeit", async () => {
    const sample = desk();
    const forfeited = {
      ...sample.recent[0],
      practice_id: 49,
      service: "Extension B1 - VOA",
      amount_idr: null,
      status: "forfeited",
    };
    const custom = {
      ...sample.recent[0],
      practice_id: 51,
      service: "Custom",
      amount_idr: 0,
      status: "no_bonus",
    };
    mocks.request.mockResolvedValue({
      sentSub: "employee-a",
      data: {
        ...sample,
        recent: [forfeited, custom],
        next_case: {
          practice_id: 50,
          service: "Extension B1 - VOA",
          amount_idr: 15000,
          already_earned: false,
          forfeits: true,
        },
      },
    });
    render(<DeliveryBonusDesk />);
    expect(
      await screen.findByText("No bonus · paid below price"),
    ).toBeVisible();
    expect(
      screen.getByText("No bonus · the client paid less than the price"),
    ).toBeVisible();
    fireEvent.click(screen.getByText("View 1 earlier delivery"));
    expect(screen.getByText("No bonus for this service")).toBeVisible();
    for (const name of [/Custom/, /Extension B1 - VOA/]) {
      const entry = screen
        .getAllByRole("link", { name })
        .map((link) => link.parentElement?.parentElement)
        .find((node) => node?.textContent?.includes("No bonus"));
      expect(entry).toBeTruthy();
      expect(within(entry as HTMLElement).queryByText(/Rp/)).toBeNull();
      expect(within(entry as HTMLElement).queryByText(/payroll/)).toBeNull();
    }
  });
  it("says a next case of a no-bonus service earns nothing", async () => {
    const sample = desk();
    mocks.request.mockResolvedValue({
      sentSub: "employee-a",
      data: {
        ...sample,
        next_case: {
          practice_id: 52,
          service: "Custom",
          amount_idr: 0,
          already_earned: false,
          forfeits: false,
        },
      },
    });
    render(<DeliveryBonusDesk />);
    expect(await screen.findByText("No bonus for this service")).toBeVisible();
    expect(screen.queryByText(/Potential bonus/)).toBeNull();
  });
  it("handles zero delivery activity and a goal save failure without fabricating earnings", async () => {
    const data = {
      ...desk(),
      next_case: null,
      milestones: 0,
      target: null,
      recent: [],
      totals: {
        pending: 0,
        approved: 0,
        paid: 0,
        awaiting_rate: 0,
        payroll_locked: 0,
      },
    };
    mocks.request.mockImplementation(async (url: string) => {
      if (url.endsWith("/delivery-goal")) throw new Error("temporary failure");
      return { sentSub: "employee-a", data };
    });
    render(<DeliveryBonusDesk />);
    expect(
      await screen.findByText(
        "Your first qualifying delivery will appear here.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("progressbar")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Set a personal goal" }),
    );
    fireEvent.change(screen.getByRole("spinbutton"), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your goal could not be saved",
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });
  it.each(["off-payroll", "request error"])(
    "stays silent before eligibility is confirmed, including %s",
    async (outcome) => {
      if (outcome === "off-payroll") {
        mocks.request.mockResolvedValue({
          sentSub: "employee-a",
          data: { team_member_id: "employee-a", on_payroll: false },
        });
      } else {
        mocks.request.mockRejectedValue(new Error("unavailable"));
      }
      const { container } = render(<DeliveryBonusDesk />);
      expect(container).toBeEmptyDOMElement();
      await waitFor(() => expect(mocks.request).toHaveBeenCalledOnce());
      expect(container).toBeEmptyDOMElement();
    },
  );
  it("clears protected figures when the goal response fails identity binding, then allows a fresh read", async () => {
    const data = { ...desk(), next_case: null };
    mocks.request.mockImplementation(async (url: string) => ({
      sentSub: url.endsWith("/delivery-goal") ? "employee-b" : "employee-a",
      data,
    }));
    render(<DeliveryBonusDesk />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Edit personal goal" }),
    );
    fireEvent.change(screen.getByRole("spinbutton"), {
      target: { value: "9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your goal update could not be verified",
    );
    expect(
      screen.queryByRole("heading", { name: "My delivery bonuses" }),
    ).toBeNull();
    expect(screen.queryAllByText("Rp 100.000")).toHaveLength(0);
    fireEvent.click(
      screen.getByRole("button", { name: "Reload my delivery bonuses" }),
    );
    expect(
      await screen.findByRole("heading", { name: "My delivery bonuses" }),
    ).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });
  it.each([
    ["document_3", "IMK — Re-entry Permit", "IMK — Re-entry Permit"],
    ["document_legacy", null, "Required permit"],
  ])(
    "uses the display label on hold %s and a neutral fallback for legacy keys",
    async (slot_key, slot_label, expected) => {
      const data = {
        ...desk(),
        next_case: null,
        holds: [
          {
            practice_id: 9,
            service: "Synthetic service",
            slot_key,
            slot_label,
            due_on: "2026-11-28",
            payout_from_month: "2026-12-01",
            next_action: "upload_permit",
            email_state: "not_sent",
          },
        ],
      };
      mocks.request.mockResolvedValue({ sentSub: "employee-a", data });
      render(<DeliveryBonusDesk />);
      expect(await screen.findByText(new RegExp(expected!))).toHaveTextContent(
        "Due 28 Nov 2026",
      );
      expect(screen.queryByText(new RegExp(slot_key!, "i"))).toBeNull();
    },
  );
});

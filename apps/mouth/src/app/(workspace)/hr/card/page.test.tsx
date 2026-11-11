import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getMyCard = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/hr/hr", () => ({ getMyCard }));

import MyCardPage from "./page";
import { AuthApi } from "@/lib/api/auth/auth.api";
import { ApiClientBase } from "@/lib/api/client";

const FILE = "1AbCdEfGhIjKlMnOpQrStUv";

const FULL = {
  employee_id: 7,
  team_member_id: "a",
  full_name: "Test Person",
  role: "Consultant",
  department: "Operations",
  hire_date: "2024-03-15",
  is_active: true,
  ptkp_status: "TK/0",
  base_salary_idr: 1,
  npwp: "00.000.000.0-000.000",
  bpjs_kesehatan_number: "KES-TEST-1",
  bpjs_ketenagakerjaan_number: "TK-TEST-1",
  bank_name: "Test Bank",
  bank_account: "ACC-TEST-1",
  bank_account_holder: "Test Holder",
  legal_name: "Legal Test Person",
  nik: "NIK-TEST-1",
  birth_place: "Testville",
  birth_date: "1990-05-02",
  gender: "F",
  address: "Line one\nLine two",
  religion: "None",
  marital_status: "Single",
  employer_entity: "PT Test Entity",
  unit: "Unit Test",
  contract: { number: "C-001", file_id: FILE },
  docs: { contract: FILE, ktp: FILE, odd_slot: FILE, bad: "x/../y" },
  end_date: null,
};

const EMPTY = {
  ...FULL,
  legal_name: null,
  nik: null,
  birth_place: null,
  birth_date: null,
  gender: null,
  address: null,
  religion: null,
  marital_status: null,
  npwp: null,
  bank_name: null,
  contract: null,
  docs: {},
  employer_entity: null,
  unit: null,
};

function signIn(who: string | null) {
  if (who) {
    localStorage.setItem(
      "user_profile",
      JSON.stringify({ id: who, email: `${who}@example.test` }),
    );
  } else {
    localStorage.removeItem("user_profile");
  }
}

/** The app's real cross-tab signal: another tab changed the stored session. */
function switchSession(who: string | null) {
  act(() => {
    signIn(who);
    window.dispatchEvent(new StorageEvent("storage", { key: "user_profile" }));
  });
}

async function openTile(name: RegExp) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name }));
  return user;
}

describe("My card page", () => {
  beforeEach(() => {
    getMyCard.mockReset();
    signIn("a");
  });

  it("renders the header and the five tiles", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    expect(
      await screen.findByRole("heading", { name: "Legal Test Person" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Consultant · Unit Test")).toBeInTheDocument();
    expect(
      screen.getByText("PT Test Entity · With us since March 2024"),
    ).toBeInTheDocument();
    for (const n of [
      /Who I am/,
      /My contract/,
      /Tax and social security/,
      /Where my salary goes/,
      /My documents/,
    ]) {
      expect(screen.getByRole("button", { name: n })).toBeInTheDocument();
    }
  });

  it("falls back to the full name and shows the end date", async () => {
    getMyCard.mockResolvedValue({
      ...FULL,
      legal_name: null,
      end_date: "2026-12-31",
    });
    render(<MyCardPage />);
    expect(
      await screen.findByRole("heading", { name: "Test Person" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Until 31 December 2026/)).toBeInTheDocument();
  });

  it("opens each tile's detail in full and Back returns with focus on the tile", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    const cases: [RegExp, string][] = [
      [/Who I am/, "NIK-TEST-1"],
      [/My contract/, "C-001"],
      [/Tax and social security/, "KES-TEST-1"],
      [/Where my salary goes/, "ACC-TEST-1"],
      [/My documents/, "Odd slot"],
    ];
    for (const [name, value] of cases) {
      const user = await openTile(name);
      const heading = screen.getByRole("heading", { level: 2 });
      expect(heading).toHaveFocus();
      expect(screen.getByText(value)).toBeInTheDocument();
      expect(
        screen.getByText(
          "You can see this, and so can the people authorised to manage HR at Bali Zero.",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText(/Only you/)).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /Back to my card/ }));
      expect(screen.getByRole("button", { name })).toHaveFocus();
    }
  });

  it("keeps the address on several lines and explains PTKP", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    await openTile(/Who I am/);
    const addr = screen.getByText(/Line one/);
    expect(addr.textContent).toBe("Line one\nLine two");
    expect(addr.className).toContain("whitespace-pre-line");
    expect(screen.getByText("Testville")).toBeInTheDocument();
    expect(screen.getByText("2 May 1990")).toBeInTheDocument();
  });

  it("explains PTKP in the tax view", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    await openTile(/Tax and social security/);
    expect(screen.getByText("TK/0")).toBeInTheDocument();
    expect(
      screen.getByText(/PTKP is the tax-free allowance/),
    ).toBeInTheDocument();
  });

  it("counts what is on file and words the gaps quietly", async () => {
    getMyCard.mockResolvedValue(EMPTY);
    render(<MyCardPage />);
    const who = await screen.findByRole("button", { name: /Who I am/ });
    expect(within(who).getByText("0 of 8 on file")).toBeInTheDocument();
    expect(
      within(screen.getByRole("button", { name: /Tax and social/ })).getByText(
        "3 of 4 on file",
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("button", { name: /My documents/ })).getByText(
        "0 of 6 on file",
      ),
    ).toBeInTheDocument();
    await openTile(/Who I am/);
    expect(
      screen.getAllByText("Not on file yet — HR will add it"),
    ).toHaveLength(8);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("counts a full card as all on file, a missing end date excluded", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    const c = await screen.findByRole("button", { name: /My contract/ });
    expect(within(c).getByText("5 of 5 on file")).toBeInTheDocument();
    const d = screen.getByRole("button", { name: /My documents/ });
    expect(within(d).getByText("3 of 8 on file")).toBeInTheDocument();
  });

  it("links only well-formed file ids, in a new tab", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    await openTile(/My documents/);
    const links = screen.getAllByRole("link", { name: "Open" });
    expect(links).toHaveLength(3);
    for (const a of links) {
      expect(a).toHaveAttribute(
        "href",
        `https://drive.google.com/file/d/${FILE}/view`,
      );
      expect(a).toHaveAttribute("target", "_blank");
      expect(a).toHaveAttribute("rel", "noreferrer");
    }
    // the "bad" slot and the missing known slots are quiet, never links
    expect(screen.getByText("Bad").closest("div.grid")).toHaveTextContent(
      "Not on file yet",
    );
    expect(screen.getAllByText("Not on file yet")).toHaveLength(5);
  });

  it("gives no contract link for an id that fails the pattern", async () => {
    getMyCard.mockResolvedValue({
      ...FULL,
      contract: { number: "C-9", file_id: "bad id/../" },
      docs: {},
    });
    render(<MyCardPage />);
    await openTile(/My contract/);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("Not on file yet")).toBeInTheDocument();
  });

  it("shows a friendly empty state on 404", async () => {
    getMyCard.mockRejectedValue(
      Object.assign(new Error("Not found"), { statusCode: 404 }),
    );
    render(<MyCardPage />);
    expect(
      await screen.findByText(
        "Your HR card has not been set up yet. HR will create it.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an error with retry for other failures", async () => {
    getMyCard.mockRejectedValueOnce(
      Object.assign(new Error("boom"), { statusCode: 500 }),
    );
    getMyCard.mockResolvedValueOnce(FULL);
    render(<MyCardPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "We could not load your card",
    );
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Legal Test Person" }),
      ).toBeInTheDocument(),
    );
    expect(getMyCard).toHaveBeenCalledTimes(2);
  });

  it("says an absent end date is not on file, never that it is open-ended", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    await openTile(/My contract/);
    expect(screen.getByText("No end date on file")).toBeInTheDocument();
    expect(screen.queryByText(/open-ended/)).not.toBeInTheDocument();
  });

  it("keys document rows by slot id so colliding labels both render", async () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    getMyCard.mockResolvedValue({
      ...FULL,
      docs: { record_x: FILE, "record-x": FILE },
    });
    render(<MyCardPage />);
    await openTile(/My documents/);
    expect(screen.getAllByText("Record x")).toHaveLength(2);
    expect(err).not.toHaveBeenCalled();
    err.mockRestore();
  });

  it("blocks Sentry Replay over every value and link", async () => {
    getMyCard.mockResolvedValue(FULL);
    const { container } = render(<MyCardPage />);
    await screen.findByRole("heading", { name: "Legal Test Person" });
    const root = container.firstElementChild as HTMLElement;
    expect(root).toHaveAttribute("data-sentry-block");
    expect(root).toHaveClass("sentry-block");
    await openTile(/My documents/);
    const inside = (el: Element) => el.closest("[data-sentry-block]") !== null;
    for (const a of screen.getAllByRole("link")) expect(inside(a)).toBe(true);
    for (const el of container.querySelectorAll("h1,h2,p,span,a,button")) {
      expect(inside(el)).toBe(true);
    }
    expect(container.children).toHaveLength(1);
  });

  it("drops the old identity's card on a session switch and fetches for the new one", async () => {
    getMyCard.mockResolvedValueOnce(FULL);
    render(<MyCardPage />);
    await screen.findByRole("heading", { name: "Legal Test Person" });
    await openTile(/Who I am/);
    expect(screen.getByText("NIK-TEST-1")).toBeInTheDocument();
    getMyCard.mockResolvedValueOnce({
      ...FULL,
      legal_name: "Second Person",
      nik: "NIK-B",
      team_member_id: "b",
    });
    switchSession("b");
    expect(screen.queryByText("NIK-TEST-1")).not.toBeInTheDocument();
    expect(screen.queryByText("Legal Test Person")).not.toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Second Person" }),
    ).toBeInTheDocument();
    expect(getMyCard).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("NIK-TEST-1")).not.toBeInTheDocument();
  });

  it("ignores a slow response that arrives after the session switched", async () => {
    let resolveA: (v: unknown) => void = () => undefined;
    getMyCard.mockImplementationOnce(() => new Promise((r) => (resolveA = r)));
    getMyCard.mockResolvedValueOnce({
      ...FULL,
      legal_name: "Second Person",
      team_member_id: "b",
    });
    render(<MyCardPage />);
    switchSession("b");
    expect(
      await screen.findByRole("heading", { name: "Second Person" }),
    ).toBeInTheDocument();
    await act(async () => {
      resolveA(FULL);
    });
    expect(screen.queryByText("Legal Test Person")).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Second Person" }),
    ).toBeInTheDocument();
  });

  it("shows no card values once signed out in another tab", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    await screen.findByRole("heading", { name: "Legal Test Person" });
    switchSession(null);
    expect(screen.queryByText("Legal Test Person")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Who I am/ }),
    ).not.toBeInTheDocument();
  });

  it("refetches on visibilitychange when the identity differs from the loaded card", async () => {
    getMyCard.mockResolvedValueOnce(FULL);
    render(<MyCardPage />);
    await screen.findByRole("heading", { name: "Legal Test Person" });
    getMyCard.mockResolvedValueOnce({
      ...FULL,
      legal_name: "Second Person",
      team_member_id: "b",
    });
    act(() => {
      signIn("b"); // changed without any storage event
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(
      await screen.findByRole("heading", { name: "Second Person" }),
    ).toBeInTheDocument();
  });

  it("writes no card value to console.* or document.title on any detail view", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map(
      (m) => vi.spyOn(console, m).mockImplementation(() => undefined),
    );
    const title = document.title;
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    for (const n of [
      /Who I am/,
      /My contract/,
      /Tax and social security/,
      /Where my salary goes/,
      /My documents/,
    ]) {
      const user = await openTile(n);
      expect(document.title).toBe(title);
      await user.click(screen.getByRole("button", { name: /Back to my card/ }));
    }
    for (const s of spies) expect(s).not.toHaveBeenCalled();
    for (const v of ["NIK-TEST-1", "ACC-TEST-1", "Legal Test Person", FILE]) {
      expect(document.title).not.toContain(v);
    }
    for (const s of spies) s.mockRestore();
  });

  it("clears the card the moment a same-tab logout starts, before the network answers", async () => {
    getMyCard.mockResolvedValue(FULL);
    render(<MyCardPage />);
    await screen.findByRole("heading", { name: "Legal Test Person" });
    // The app's REAL logout: AuthApi.logout -> ApiClientBase.clearToken, with the
    // server call left pending (the workspace layout waits on it before navigating).
    const client = new ApiClientBase("https://api.test");
    vi.spyOn(client, "request").mockImplementation(
      () => new Promise<never>(() => undefined),
    );
    void new AuthApi(client).logout();
    await waitFor(() =>
      expect(screen.queryByText("Legal Test Person")).not.toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: /Who I am/ }),
    ).not.toBeInTheDocument();
    expect(localStorage.getItem("user_profile")).toBeNull();
  });

  it("does not show a card that names another team member, even with the profile unchanged", async () => {
    getMyCard.mockResolvedValue({ ...FULL, team_member_id: "someone-else" });
    render(<MyCardPage />);
    expect(
      await screen.findByText("Your session changed. Reload the page."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Legal Test Person")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Who I am/ }),
    ).not.toBeInTheDocument();
  });

  it("token-only removal then a response for another member: nothing shown", async () => {
    localStorage.setItem("auth_token", "tok");
    let resolve: (v: unknown) => void = () => undefined;
    getMyCard.mockImplementationOnce(() => new Promise((r) => (resolve = r)));
    render(<MyCardPage />);
    localStorage.removeItem("auth_token"); // profile A stays; the cookie now says B
    await act(async () => {
      resolve({ ...FULL, team_member_id: "b", legal_name: "Other Person" });
    });
    expect(screen.queryByText("Other Person")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Who I am/ }),
    ).not.toBeInTheDocument();
  });

  it("shows the card when the response names the signed-in member", async () => {
    getMyCard.mockResolvedValue({ ...FULL, team_member_id: "a" });
    render(<MyCardPage />);
    expect(
      await screen.findByRole("heading", { name: "Legal Test Person" }),
    ).toBeInTheDocument();
  });

  it("shows nothing and asks nothing when the signed-in identity is unreadable", async () => {
    localStorage.setItem("user_profile", "{not json");
    getMyCard.mockResolvedValue(FULL);
    const { container } = render(<MyCardPage />);
    await act(async () => undefined);
    expect(getMyCard).not.toHaveBeenCalled();
    expect(screen.queryByText("Legal Test Person")).not.toBeInTheDocument();
    expect(container.textContent).toBe("");
  });
});

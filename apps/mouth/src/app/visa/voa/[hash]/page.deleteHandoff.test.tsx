import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import VoaResultPage from "./page";
import { readCheckoutHandoff, writeCheckoutHandoff } from "../checkoutHandoff";

vi.mock("@balizero/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@balizero/core")>();
  return {
    ...actual,
    useFunnelApp: () => ({
      viewed: vi.fn(),
      resultViewed: vi.fn(),
      ctaClicked: vi.fn(),
      whatsappHandoff: vi.fn(),
      shareClicked: vi.fn(),
      emailSubscribed: vi.fn(),
      formSubmitFailed: vi.fn(),
    }),
  };
});

const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
const HASH = "opaque-test-hash";
const VALUES = { full_name: "Person One", passport_number: "X0000001" };

async function openDeleteConfirm() {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () => ({
      verdict: "ACCEPT",
      reason_codes: [],
      price_idr: 790000,
    }),
  });
  render(<VoaResultPage params={Promise.resolve({ hash: HASH })} />);
  await waitFor(() =>
    expect(screen.getByTestId("bz-stamp")).toBeInTheDocument(),
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /delete this check/i }));
  return user;
}

describe("VoaResultPage — deleting the check drops the handed-off traveller values", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    window.localStorage.clear();
    fetchMock.mockReset();
  });

  it("a successful deletion removes the handoff stored for that check", async () => {
    writeCheckoutHandoff(HASH, VALUES);
    const user = await openDeleteConfirm();

    fetchMock.mockResolvedValueOnce({ status: 204 });
    await user.click(screen.getByRole("button", { name: /yes, delete/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/this check has been deleted/i),
      ).toBeInTheDocument(),
    );
    expect(readCheckoutHandoff(HASH)).toEqual({
      full_name: undefined,
      passport_number: undefined,
    });
  });

  it("a failed deletion leaves the handoff in place", async () => {
    writeCheckoutHandoff(HASH, VALUES);
    const user = await openDeleteConfirm();

    fetchMock.mockResolvedValueOnce({ status: 503 });
    await user.click(screen.getByRole("button", { name: /yes, delete/i }));

    await screen.findByRole("alert");
    expect(readCheckoutHandoff(HASH)).toEqual(VALUES);
  });

  it("a successful deletion with no handoff stored does not throw", async () => {
    const user = await openDeleteConfirm();

    fetchMock.mockResolvedValueOnce({ status: 204 });
    await user.click(screen.getByRole("button", { name: /yes, delete/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/this check has been deleted/i),
      ).toBeInTheDocument(),
    );
  });
});

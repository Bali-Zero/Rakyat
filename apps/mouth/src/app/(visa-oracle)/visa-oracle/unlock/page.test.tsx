import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import VisaOracleUnlockPage from "./page";

const LOCK = /lock this browser again/i;
const ERROR_TEXT = /could not be locked/i;

function stubLocation() {
  const assign = vi.fn();
  vi.stubGlobal("location", { ...window.location, assign });
  return assign;
}

async function clickLock() {
  fireEvent.click(screen.getByRole("button", { name: LOCK }));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("visa oracle unlock page: lock this browser again", () => {
  let assign: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    assign = stubLocation();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("guilt: a DELETE answering 500 does not redirect and shows an error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 500 }),
    );
    render(<VisaOracleUnlockPage />);

    await clickLock();

    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByText(ERROR_TEXT)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: LOCK })).toBeEnabled();
  });

  it("guilt: a DELETE that rejects does not redirect and shows an error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<VisaOracleUnlockPage />);

    await clickLock();

    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByText(ERROR_TEXT)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: LOCK })).toBeEnabled();
  });

  it.each([200, 204])(
    "innocence: a DELETE answering %i redirects to the oracle",
    async (status) => {
      const fetchMock = vi.fn().mockResolvedValue({ ok: true, status });
      vi.stubGlobal("fetch", fetchMock);
      render(<VisaOracleUnlockPage />);

      await clickLock();

      expect(fetchMock).toHaveBeenCalledWith(
        "/api/visa-oracle-unlock",
        expect.objectContaining({ method: "DELETE" }),
      );
      expect(assign).toHaveBeenCalledWith("/visa-oracle");
      expect(screen.queryByText(ERROR_TEXT)).toBeNull();
    },
  );
});

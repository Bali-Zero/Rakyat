/**
 * A Drive connection that did not complete says so.
 *
 * The OAuth callback sends a failed connection back to this page with
 * `?error=<code>`. The page read only `?success=`, so a refused or failed
 * connection showed "Disconnected", or a stale "Connected", and nothing else.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import IntegrationsPage from "./page";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  get: vi.fn(),
  getAuthUrl: vi.fn(),
  error: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push }),
}));
vi.mock("@/lib/api", () => ({
  api: { get: mocks.get, drive: { getAuthUrl: mocks.getAuthUrl } },
}));
vi.mock("@/lib/logger", () => ({ logger: { error: mocks.error } }));

const DID_NOT_COMPLETE = "The Google Drive connection did not complete.";

function arriveAt(query: string) {
  window.history.replaceState({}, "", `/settings/integrations${query}`);
}

async function shown(): Promise<string | null> {
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));
  await screen.findByText(/^(Connected|Disconnected|Error)$/);
  return screen.queryByRole("alert")?.textContent ?? null;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.get.mockResolvedValue({ connected: false, configured: true });
});

describe("the callback's three codes", () => {
  it.each([
    [
      "oauth_denied",
      `${DID_NOT_COMPLETE} Google did not grant access. You can try again.`,
    ],
    [
      "token_exchange_failed",
      `${DID_NOT_COMPLETE} The last step with Google failed. You can try again.`,
    ],
    [
      "system_token_failed",
      "The shared Google Drive connection did not complete. The last step with Google failed.",
    ],
  ])("%s is told in its own sentence", async (code, sentence) => {
    arriveAt(`?error=${code}`);

    render(<IntegrationsPage />);

    expect(await shown()).toBe(sentence);
    expect(window.location.pathname).toBe("/settings/integrations");
    expect(window.location.search).toBe("");
  });

  it.each([
    ["still connected from before", true, "Reconnect"],
    ["not connected", false, "Connect"],
  ])(
    "the sentence stands beside the real state of the connection: %s",
    async (_label, connected, button) => {
      mocks.get.mockResolvedValue({ connected, configured: true });
      arriveAt("?error=oauth_denied");

      render(<IntegrationsPage />);

      expect(await shown()).toContain(DID_NOT_COMPLETE);
      expect(screen.getByRole("button", { name: button })).toBeEnabled();
      expect(mocks.get).toHaveBeenCalledWith(
        "/api/integrations/google-drive/status",
      );
    },
  );
});

describe("a code the page does not know", () => {
  it.each([
    ["an unknown code", "something_new"],
    ["an empty value", ""],
    ["a name every object has", "constructor"],
    ["a sentence of the sender's own", "Call this number to finish"],
    ["markup", "<b>Sign in again</b>"],
  ])("%s gets the plain sentence and none of its own text", async (_l, raw) => {
    arriveAt(`?error=${encodeURIComponent(raw)}`);

    render(<IntegrationsPage />);

    expect(await shown()).toBe(DID_NOT_COMPLETE);
    if (raw) expect(document.body.textContent).not.toContain(raw);
    expect(window.location.search).toBe("");
  });
});

describe("what does not change", () => {
  it.each([
    ["no parameter", ""],
    ["a completed connection", "?success=google_drive_connected"],
    ["a completed shared connection", "?success=system_drive_connected"],
  ])("%s shows no failure", async (_label, query) => {
    arriveAt(query);

    render(<IntegrationsPage />);

    expect(await shown()).toBeNull();
    expect(window.location.search).toBe("");
  });

  it("another parameter alone is left in the address", async () => {
    arriveAt("?from=settings");

    render(<IntegrationsPage />);

    expect(await shown()).toBeNull();
    expect(window.location.search).toBe("?from=settings");
  });

  it("a failure wins over a success in the same address", async () => {
    arriveAt("?success=google_drive_connected&error=oauth_denied");

    render(<IntegrationsPage />);

    expect(await shown()).toContain("Google did not grant access");
    expect(window.location.search).toBe("");
  });
});

describe("only what the page has read leaves the address", () => {
  it.each([
    ["after it", "?error=oauth_denied&from=settings", "?from=settings"],
    ["before it", "?from=settings&error=oauth_denied", "?from=settings"],
    [
      "on both sides",
      "?from=settings&error=token_exchange_failed&tab=drive",
      "?from=settings&tab=drive",
    ],
    [
      "beside an unknown code",
      "?error=something_new&from=settings",
      "?from=settings",
    ],
    ["beside an empty code", "?error=&from=settings", "?from=settings"],
    [
      "beside a code given twice",
      "?error=oauth_denied&from=settings&error=x",
      "?from=settings",
    ],
  ])(
    "a failure with another parameter %s keeps that parameter",
    async (_label, query, left) => {
      arriveAt(query);

      render(<IntegrationsPage />);

      expect(await shown()).toContain(DID_NOT_COMPLETE);
      expect(window.location.pathname).toBe("/settings/integrations");
      expect(window.location.search).toBe(left);
    },
  );

  it.each([
    ["?success=google_drive_connected&from=settings", "?from=settings"],
    [
      "?from=settings&success=system_drive_connected&tab=drive",
      "?from=settings&tab=drive",
    ],
  ])(
    "a completed connection in %s keeps the other parameters",
    async (query, left) => {
      arriveAt(query);

      render(<IntegrationsPage />);

      expect(await shown()).toBeNull();
      expect(window.location.search).toBe(left);
    },
  );

  it("the value of another parameter is kept as it was meant", async () => {
    arriveAt("?note=a%20b%26c&error=oauth_denied");

    render(<IntegrationsPage />);

    expect(await shown()).toContain(DID_NOT_COMPLETE);
    const left = new URLSearchParams(window.location.search);
    expect([...left.keys()]).toEqual(["note"]);
    expect(left.get("note")).toBe("a b&c");
  });

  it.each([
    ["a failure", "?error=oauth_denied#drive", ""],
    [
      "a failure beside another parameter",
      "?error=oauth_denied&from=settings#drive",
      "?from=settings",
    ],
    ["a completed connection", "?success=google_drive_connected#drive", ""],
  ])("the fragment stays after %s", async (_label, address, left) => {
    arriveAt(address);

    render(<IntegrationsPage />);

    await shown();
    expect(window.location.search).toBe(left);
    expect(window.location.hash).toBe("#drive");
  });

  it("a success value the page does not know is not its to remove", async () => {
    arriveAt("?error=oauth_denied&success=something_else");

    render(<IntegrationsPage />);

    expect(await shown()).toContain("Google did not grant access");
    expect(window.location.search).toBe("?success=something_else");
  });

  it("an address with nothing for this page is not rewritten at all", async () => {
    arriveAt("?success=something_else&from=settings#drive");
    const rewrite = vi.spyOn(window.history, "replaceState");

    render(<IntegrationsPage />);

    expect(await shown()).toBeNull();
    expect(rewrite).not.toHaveBeenCalled();
    expect(window.location.search).toBe(
      "?success=something_else&from=settings",
    );
    expect(window.location.hash).toBe("#drive");
    rewrite.mockRestore();
  });
});

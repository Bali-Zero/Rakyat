import React, { useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Client } from "@/lib/api/crm/crm.types";
import { ClientPicker } from "./ClientPicker";

const { mockGetClients, mockLogError } = vi.hoisted(() => ({
  mockGetClients: vi.fn(),
  mockLogError: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: { crm: { getClients: mockGetClients } },
}));
vi.mock("@/lib/logger", () => ({
  logger: {
    error: mockLogError,
    warn: vi.fn(),
    info: vi.fn(),
    debug: vi.fn(),
  },
}));

const client = (id: number, name: string): Client =>
  ({ id, full_name: name, email: `c${id}@example.test` }) as Client;

function Harness() {
  const [selected, setSelected] = useState<Client | null>(null);
  return (
    <ClientPicker
      selectedClient={selected}
      onSelect={setSelected}
      onClear={() => setSelected(null)}
    />
  );
}

const settle = (ms = 450) =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });

describe("ClientPicker search", () => {
  beforeEach(() => {
    mockGetClients.mockReset();
    mockLogError.mockClear();
  });

  it("searches with the text as typed and lists what the backend returns", async () => {
    mockGetClients.mockResolvedValue([client(1, "Müller Anna")]);
    render(<Harness />);
    await userEvent
      .setup()
      .type(screen.getByLabelText("Search client"), "muller");
    expect(await screen.findByText("Müller Anna")).toBeInTheDocument();
    expect(mockGetClients).toHaveBeenCalledWith({
      search: "muller",
      limit: 20,
    });
  });

  it("an older response that arrives after a newer one is dropped", async () => {
    let resolveOld: (v: Client[]) => void = () => {};
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((resolve) => (resolveOld = resolve)),
    );
    mockGetClients.mockResolvedValueOnce([client(2, "Synthetic Beta")]);
    render(<Harness />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("Search client");
    await user.type(input, "ma");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
    await user.type(input, "ri");
    await screen.findByText("Synthetic Beta");
    await act(async () => {
      resolveOld([client(1, "Synthetic Alpha")]);
    });
    expect(screen.queryByText("Synthetic Alpha")).toBeNull();
    expect(screen.getByText("Synthetic Beta")).toBeInTheDocument();
  });

  it("clearing the text while a request is in flight leaves no list", async () => {
    let resolveOld: (v: Client[]) => void = () => {};
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((resolve) => (resolveOld = resolve)),
    );
    render(<Harness />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("Search client");
    await user.type(input, "ma");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
    expect(document.querySelector(".animate-spin")).not.toBeNull();
    await user.clear(input);
    expect(document.querySelector(".animate-spin")).toBeNull();
    await act(async () => {
      resolveOld([client(1, "Synthetic Alpha")]);
    });
    expect(screen.queryByText("Synthetic Alpha")).toBeNull();
    expect(document.querySelector(".animate-spin")).toBeNull();
  });

  it("a failing request leaves no list, not the previous text's list", async () => {
    mockGetClients.mockResolvedValueOnce([client(1, "Synthetic Alpha")]);
    mockGetClients.mockRejectedValue(new Error("boom"));
    render(<Harness />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("Search client");
    await user.type(input, "ma");
    await screen.findByText("Synthetic Alpha");
    await user.type(input, "b");
    await waitFor(() => expect(mockLogError).toHaveBeenCalled());
    expect(screen.queryByText("Synthetic Alpha")).toBeNull();
  });

  it("edit and revert shows no list until a request for the current text succeeds", async () => {
    mockGetClients.mockResolvedValueOnce([client(1, "Synthetic Alpha")]);
    mockGetClients.mockImplementation(() => new Promise(() => {}));
    render(<Harness />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("Search client");
    await user.type(input, "ma");
    await screen.findByText("Synthetic Alpha");
    await user.type(input, "b");
    await user.type(input, "{Backspace}");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Synthetic Alpha")).toBeNull();
  });

  it("a response that arrives after unmount does nothing", async () => {
    let resolveOld: (v: Client[]) => void = () => {};
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((resolve) => (resolveOld = resolve)),
    );
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const { unmount } = render(<Harness />);
    await userEvent.setup().type(screen.getByLabelText("Search client"), "ma");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => {
      resolveOld([client(1, "Synthetic Alpha")]);
    });
    await settle(50);
    expect(consoleError).not.toHaveBeenCalled();
    expect(mockLogError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("a response or failure that arrives after unmount does nothing", async () => {
    let rejectOld: (e: Error) => void = () => {};
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((_, reject) => (rejectOld = reject)),
    );
    const { unmount } = render(<Harness />);
    await userEvent.setup().type(screen.getByLabelText("Search client"), "ma");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => {
      rejectOld(new Error("late"));
    });
    await settle(50);
    expect(mockLogError).not.toHaveBeenCalled();
  });

  it("revert to an earlier text ignores the original request and shows the current one", async () => {
    let resolveFirst: (v: Client[]) => void = () => {};
    let resolveSecond: (v: Client[]) => void = () => {};
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((resolve) => (resolveFirst = resolve)),
    );
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((resolve) => (resolveSecond = resolve)),
    );
    render(<Harness />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("Search client");
    await user.type(input, "ma");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
    await user.type(input, "b");
    await user.type(input, "{Backspace}");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolveFirst([client(1, "Synthetic Original")]);
    });
    expect(screen.queryByText("Synthetic Original")).toBeNull();
    await act(async () => {
      resolveSecond([client(2, "Synthetic Current")]);
    });
    expect(await screen.findByText("Synthetic Current")).toBeInTheDocument();
  });

  it("closing the list mid-request drops the response and a late failure; reopening searches again", async () => {
    let resolveFirst: (v: Client[]) => void = () => {};
    let rejectSecond: (e: Error) => void = () => {};
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((resolve) => (resolveFirst = resolve)),
    );
    mockGetClients.mockResolvedValueOnce([client(3, "Synthetic Reopened")]);
    render(<Harness />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("Search client");
    await user.type(input, "ma");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
    await user.click(document.body);
    await act(async () => {
      resolveFirst([client(1, "Synthetic Closed")]);
    });
    expect(screen.queryByText("Synthetic Closed")).toBeNull();
    expect(document.querySelector(".animate-spin")).toBeNull();
    await user.click(input);
    expect(await screen.findByText("Synthetic Reopened")).toBeInTheDocument();
    expect(mockGetClients).toHaveBeenCalledTimes(2);
    expect(rejectSecond).toBeDefined();
  });

  it("a failure that arrives after the list was closed is not reported", async () => {
    let rejectFirst: (e: Error) => void = () => {};
    mockGetClients.mockImplementationOnce(
      () => new Promise<Client[]>((_, reject) => (rejectFirst = reject)),
    );
    render(<Harness />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Search client"), "ma");
    await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
    await user.click(document.body);
    await act(async () => {
      rejectFirst(new Error("late"));
    });
    await settle(50);
    expect(mockLogError).not.toHaveBeenCalled();
  });
});

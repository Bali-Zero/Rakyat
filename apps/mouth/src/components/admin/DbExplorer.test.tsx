import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

import { DbExplorer } from "./DbExplorer";

const { getPostgresTables, getTableData } = vi.hoisted(() => ({
  getPostgresTables: vi.fn(),
  getTableData: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: { getPostgresTables, getTableData },
}));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));
vi.mock("@/components/ui/select", () => ({
  Select: ({
    onValueChange,
    children,
  }: {
    onValueChange?: (value: string) => void;
    children?: ReactNode;
  }) => (
    <div>
      {children}
      <button onClick={() => onValueChange?.("table_a")}>table_a</button>
      <button onClick={() => onValueChange?.("table_b")}>table_b</button>
    </div>
  ),
  SelectTrigger: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectItem: ({ children }: { children?: ReactNode }) => <>{children}</>,
}));

const data = (value: string, total_rows = 1) => ({
  table: value,
  total_rows,
  columns: ["value"],
  rows: [{ value }],
});

function held<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function refreshButton(container: HTMLElement): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>("button.ml-auto");
  if (!button) throw new Error("refresh button not found");
  return button;
}

beforeEach(() => {
  vi.resetAllMocks();
  getPostgresTables.mockResolvedValue(["table_a", "table_b"]);
});

async function select(name: string) {
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name }));
  });
}

describe("DbExplorer", () => {
  it("never shows the previous table's rows while the new one loads, and names a failed table", async () => {
    const b = held<ReturnType<typeof data>>();
    getTableData
      .mockResolvedValueOnce(data("A value"))
      .mockReturnValueOnce(b.promise);
    render(<DbExplorer />);
    await select("table_a");
    expect(screen.getByText("A value")).toBeInTheDocument();

    await select("table_b");
    expect(screen.queryByText("A value")).not.toBeInTheDocument();

    await act(async () => b.reject(new Error("fixture failure")));
    expect(screen.queryByText("A value")).not.toBeInTheDocument();
    expect(screen.getByText("Could not load table_b.")).toBeInTheDocument();
  });

  it("clears the rows and names the table when a refresh of it fails", async () => {
    getTableData
      .mockResolvedValueOnce(data("A value"))
      .mockRejectedValueOnce(new Error("fixture failure"));
    const { container } = render(<DbExplorer />);
    await select("table_a");
    expect(screen.getByText("A value")).toBeInTheDocument();

    await act(async () => fireEvent.click(refreshButton(container)));
    expect(screen.queryByText("A value")).not.toBeInTheDocument();
    expect(screen.getByText("Could not load table_a.")).toBeInTheDocument();
  });

  it("ignores an older answer that arrives after the newest", async () => {
    const a = held<ReturnType<typeof data>>();
    getTableData
      .mockReturnValueOnce(a.promise)
      .mockResolvedValueOnce(data("B value", 51));
    render(<DbExplorer />);
    await select("table_a");
    await select("table_b");
    expect(screen.getByText("B value")).toBeInTheDocument();

    await act(async () => a.resolve(data("A value")));
    expect(screen.queryByText("A value")).not.toBeInTheDocument();
    expect(screen.getByText("B value")).toBeInTheDocument();
    expect(screen.getByText("Rows 1-50 of 51")).toBeInTheDocument();
  });

  it("ignores an older failure that arrives after the newest", async () => {
    const a = held<ReturnType<typeof data>>();
    getTableData
      .mockReturnValueOnce(a.promise)
      .mockResolvedValueOnce(data("B value"));
    render(<DbExplorer />);
    await select("table_a");
    await select("table_b");

    await act(async () => a.reject(new Error("fixture failure")));
    expect(screen.getByText("B value")).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/)).not.toBeInTheDocument();
  });

  it("stays loading while the newest request is in flight, whatever an older one does", async () => {
    const a = held<ReturnType<typeof data>>();
    const b = held<ReturnType<typeof data>>();
    getTableData.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    const { container } = render(<DbExplorer />);
    await select("table_a");
    await select("table_b");

    await act(async () => a.resolve(data("A value")));
    expect(refreshButton(container)).toBeDisabled();

    await act(async () => b.resolve(data("B value")));
    expect(refreshButton(container)).toBeEnabled();
  });

  it("shows the rows of one selection, and of a refresh after an error", async () => {
    getTableData
      .mockResolvedValueOnce(data("Loaded value"))
      .mockRejectedValueOnce(new Error("fixture failure"))
      .mockResolvedValueOnce(data("Recovered value"));
    const { container } = render(<DbExplorer />);
    await select("table_a");
    expect(screen.getByText("Loaded value")).toBeInTheDocument();

    await act(async () => fireEvent.click(refreshButton(container)));
    expect(screen.getByText("Could not load table_a.")).toBeInTheDocument();
    await act(async () => fireEvent.click(refreshButton(container)));
    expect(screen.getByText("Recovered value")).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/)).not.toBeInTheDocument();
  });
});

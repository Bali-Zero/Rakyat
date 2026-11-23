import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

import { VectorExplorer } from "./VectorExplorer";

const { getQdrantCollections, getQdrantPoints } = vi.hoisted(() => ({
  getQdrantCollections: vi.fn(),
  getQdrantPoints: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: { getQdrantCollections, getQdrantPoints },
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
      <button onClick={() => onValueChange?.("collection_a")}>
        collection_a
      </button>
      <button onClick={() => onValueChange?.("collection_b")}>
        collection_b
      </button>
    </div>
  ),
  SelectTrigger: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectItem: ({ children }: { children?: ReactNode }) => <>{children}</>,
}));

const points = (value: string) => ({
  points: [{ id: value, payload: { value } }],
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
  getQdrantCollections.mockResolvedValue({
    collections: [
      { name: "collection_a", status: "green" },
      { name: "collection_b", status: "green" },
    ],
  });
});

async function select(name: string) {
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name }));
  });
}

describe("VectorExplorer", () => {
  it("never shows the previous collection's points while the new one loads, and names a failed collection", async () => {
    const b = held<ReturnType<typeof points>>();
    getQdrantPoints
      .mockResolvedValueOnce(points("A point"))
      .mockReturnValueOnce(b.promise);
    render(<VectorExplorer />);
    await select("collection_a");
    expect(screen.getByText(/ID: A point/)).toBeInTheDocument();

    await select("collection_b");
    expect(screen.queryByText(/ID: A point/)).not.toBeInTheDocument();

    await act(async () => b.reject(new Error("fixture failure")));
    expect(screen.queryByText(/ID: A point/)).not.toBeInTheDocument();
    expect(
      screen.getByText("Could not load collection_b."),
    ).toBeInTheDocument();
  });

  it("clears the points and names the collection when a refresh of it fails", async () => {
    getQdrantPoints
      .mockResolvedValueOnce(points("A point"))
      .mockRejectedValueOnce(new Error("fixture failure"));
    const { container } = render(<VectorExplorer />);
    await select("collection_a");
    expect(screen.getByText(/ID: A point/)).toBeInTheDocument();

    await act(async () => fireEvent.click(refreshButton(container)));
    expect(screen.queryByText(/ID: A point/)).not.toBeInTheDocument();
    expect(
      screen.getByText("Could not load collection_a."),
    ).toBeInTheDocument();
  });

  it("ignores an older answer that arrives after the newest", async () => {
    const a = held<ReturnType<typeof points>>();
    getQdrantPoints
      .mockReturnValueOnce(a.promise)
      .mockResolvedValueOnce(points("B point"));
    render(<VectorExplorer />);
    await select("collection_a");
    await select("collection_b");
    expect(screen.getByText(/ID: B point/)).toBeInTheDocument();

    await act(async () => a.resolve(points("A point")));
    expect(screen.queryByText(/ID: A point/)).not.toBeInTheDocument();
    expect(screen.getByText(/ID: B point/)).toBeInTheDocument();
  });

  it("ignores an older failure that arrives after the newest", async () => {
    const a = held<ReturnType<typeof points>>();
    getQdrantPoints
      .mockReturnValueOnce(a.promise)
      .mockResolvedValueOnce(points("B point"));
    render(<VectorExplorer />);
    await select("collection_a");
    await select("collection_b");

    await act(async () => a.reject(new Error("fixture failure")));
    expect(screen.getByText(/ID: B point/)).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/)).not.toBeInTheDocument();
  });

  it("stays loading while the newest request is in flight, whatever an older one does", async () => {
    const a = held<ReturnType<typeof points>>();
    const b = held<ReturnType<typeof points>>();
    getQdrantPoints
      .mockReturnValueOnce(a.promise)
      .mockReturnValueOnce(b.promise);
    const { container } = render(<VectorExplorer />);
    await select("collection_a");
    await select("collection_b");

    await act(async () => a.resolve(points("A point")));
    expect(refreshButton(container)).toBeDisabled();

    await act(async () => b.resolve(points("B point")));
    expect(refreshButton(container)).toBeEnabled();
  });

  it("shows the points of one selection, and of a refresh after an error", async () => {
    getQdrantPoints
      .mockResolvedValueOnce(points("Loaded point"))
      .mockRejectedValueOnce(new Error("fixture failure"))
      .mockResolvedValueOnce(points("Recovered point"));
    const { container } = render(<VectorExplorer />);
    await select("collection_a");
    expect(screen.getByText(/ID: Loaded point/)).toBeInTheDocument();

    await act(async () => fireEvent.click(refreshButton(container)));
    expect(
      screen.getByText("Could not load collection_a."),
    ).toBeInTheDocument();
    await act(async () => fireEvent.click(refreshButton(container)));
    expect(screen.getByText(/ID: Recovered point/)).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/)).not.toBeInTheDocument();
  });
});

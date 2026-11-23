import { describe, it, expect, vi } from "vitest";
import {
  render as rtlRender,
  screen,
  fireEvent,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ClientKanban } from "../ClientKanban";
import type { Client } from "@/lib/api/crm/crm.types";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(() => ({
    getVirtualItems: () => [],
    getTotalSize: () => 0,
  })),
}));

const client = (id: number, status: string): Client =>
  ({
    id,
    uuid: `uuid-${id}`,
    full_name: `Client ${id}`,
    status,
    client_type: "individual",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  }) as Client;

function render(ui: React.ReactElement) {
  return rtlRender(
    <QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>,
  );
}

function drag(name: string) {
  const card = screen.getByText(name).closest('[draggable="true"]');
  fireEvent.dragStart(card as HTMLElement, {
    dataTransfer: { setData: vi.fn(), setDragImage: vi.fn() },
  });
}

function drop(column: HTMLElement, id: number) {
  fireEvent.drop(column, {
    dataTransfer: { getData: () => String(id), setData: vi.fn() },
  });
}

describe("ClientKanban statuses", () => {
  it("INNOCENCE: a client stored as completed or lost still has its column", () => {
    render(
      <ClientKanban
        clients={[client(1, "completed"), client(2, "lost")]}
        onStatusChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("Lost")).toBeInTheDocument();
  });

  it("GUILT: a client stored as prospect is shown in a column of its own", () => {
    render(
      <ClientKanban
        clients={[client(3, "prospect"), client(4, "lead")]}
        onStatusChange={vi.fn()}
      />,
    );
    const col = screen
      .getByText("Prospects")
      .closest(".ledger-container") as HTMLElement;
    expect(within(col).getByText("Client 3")).toBeInTheDocument();
    expect(within(col).queryByText("Client 4")).toBeNull();
  });

  it("INNOCENCE: dropping on the prospect column sends prospect", () => {
    const onStatusChange = vi.fn().mockResolvedValue(undefined);
    render(
      <ClientKanban
        clients={[client(1, "lead")]}
        onStatusChange={onStatusChange}
      />,
    );
    drag("Client 1");
    drop(
      screen.getByText("Prospects").closest(".ledger-container") as HTMLElement,
      1,
    );
    expect(onStatusChange).toHaveBeenCalledWith(1, "prospect");
  });

  it("GUILT: dropping a card on a non-writable column sends no request", () => {
    const onStatusChange = vi.fn().mockResolvedValue(undefined);
    render(
      <ClientKanban
        clients={[client(1, "lead")]}
        onStatusChange={onStatusChange}
      />,
    );
    const col = (t: string) =>
      screen.getByText(t).closest(".ledger-container") as HTMLElement;
    drag("Client 1");
    drop(col("Completed"), 1);
    drag("Client 1");
    drop(col("Lost"), 1);
    expect(onStatusChange).not.toHaveBeenCalled();
  });

  it("INNOCENCE: dropping on a writable column still changes the status", () => {
    const onStatusChange = vi.fn().mockResolvedValue(undefined);
    render(
      <ClientKanban
        clients={[client(1, "lead")]}
        onStatusChange={onStatusChange}
      />,
    );
    drag("Client 1");
    drop(
      screen.getByText("Active").closest(".ledger-container") as HTMLElement,
      1,
    );
    expect(onStatusChange).toHaveBeenCalledWith(1, "active");
  });
});

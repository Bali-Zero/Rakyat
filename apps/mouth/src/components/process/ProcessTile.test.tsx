import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Practice } from "@/lib/api/crm/crm.types";
import { ProcessTile } from "./ProcessTile";

const sample: Practice = {
  id: 41,
  client_id: 90,
  client_name: "Example Client",
  practice_type_id: 1,
  practice_type_name: "Tax filing",
  assignee_name: "Test Operator",
  assignee_department: "tax",
  status: "on_process",
  priority: "normal",
  payment_status: "paid",
  created_at: "2026-01-08",
};

function tile(patch: Partial<Practice> = {}) {
  const openClient = vi.fn();
  const openProcess = vi.fn();
  render(
    <ProcessTile
      practice={{ ...sample, ...patch }}
      updating={false}
      openClient={openClient}
      openProcess={openProcess}
      openMenu={vi.fn()}
      onContextMenu={vi.fn()}
      deliveryPeek={
        <details>
          <summary>Delivery documents</summary>
        </details>
      }
    />,
  );
  return { openClient, openProcess };
}

afterEach(() => vi.useRealTimers());

describe("ProcessTile", () => {
  it("distinguishes a recorded payment from the quote and fee", () => {
    tile({
      quoted_price: 500_000,
      actual_price: 450_000,
      paid_amount: 100_000,
      payment_status: "partial",
    });
    expect(screen.getByText("Received")).toBeVisible();
    expect(screen.getByText("Rp 100K")).toBeVisible();
    expect(screen.getByText("Part paid")).toBeVisible();
  });

  it("shows a quoted figure with its own label when no receipt or agreed fee exists", () => {
    tile({ quoted_price: 500_000, paid_amount: 0, payment_status: "unpaid" });
    expect(screen.getByText("Quoted")).toBeVisible();
    expect(screen.getByText("Rp 500K")).toBeVisible();
    expect(screen.queryByText("Received")).toBeNull();
  });

  it("shows the real close date and keeps both navigation targets accessible", () => {
    const { openClient, openProcess } = tile({
      status: "completed",
      completion_date: "2026-01-08T16:05:00Z",
      paid_amount: 100_000,
    });
    expect(screen.getByText("9 Jan 2026")).toBeVisible();
    expect(screen.getByText("Delivery documents")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Example Client, open client profile",
      }),
    );
    expect(openClient).toHaveBeenCalledOnce();
    expect(openProcess).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Open process 41: Tax filing" }),
    );
    expect(openProcess).toHaveBeenCalledOnce();
  });

  it("counts expiry dates relative to the Bali work day, including just after midnight", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-01-08T16:05:00Z"));
    tile({ expiry_date: "2026-01-09", paid_amount: 100_000 });
    expect(screen.getByText("Expires today")).toBeVisible();
    expect(screen.getByText("9 Jan 2026")).toBeVisible();
  });
});

/**
 * The confirm dialog must paint an opaque panel with colours this app's theme
 * defines. All names and amounts are invented.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi, describe, it, expect } from "vitest";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
import { ConfirmModal } from "../_components/ConfirmModal";
import { candidate } from "./fixtures";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: { confirm: vi.fn() },
}));

const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");

function renderModal(over: Parameters<typeof candidate>[0] = {}) {
  return render(
    <ConfirmModal
      txn={{
        id: 1,
        amount_idr: 500000,
        description: "TRF CONTOH",
        txn_date: "2026-01-15",
      }}
      candidate={candidate(over)}
      onClose={vi.fn()}
      onConfirmed={vi.fn()}
    />,
  );
}

describe("ConfirmModal theme", () => {
  it("paints the panel with a background variable the theme defines", () => {
    renderModal();
    const panel = screen
      .getByText("Confirm payment match")
      .closest(".max-w-md") as HTMLElement;
    expect(panel).not.toBeNull();
    const bg = Array.from(panel.classList).find((c) =>
      /^bg-\[var\(--[\w-]+\)\]$/.test(c),
    );
    expect(bg).toBeDefined();
    const name = /var\((--[\w-]+)\)/.exec(bg as string)![1];
    expect(css).toMatch(new RegExp(`^\\s*${name}\\s*:`, "m"));
  });
});

describe("ConfirmModal identity, statement and toast", () => {
  const full = {
    invoice_number: "INV-000000-00001",
    invoice_issued_at: "2026-01-10T00:00:00+00:00",
    service_name: "Example service",
  };

  it("shows the number, the issue date and the service", () => {
    renderModal(full);
    const id = screen.getByTestId("candidate-identity");
    expect(id).toHaveTextContent(
      "INV-000000-00001 · 2026-01-10 · Example service",
    );
  });

  it("shows No invoice number, and no date or service, for null and for absent fields", () => {
    const { unmount } = renderModal({
      invoice_number: null,
      invoice_issued_at: null,
      service_name: null,
    });
    expect(screen.getByTestId("candidate-identity").textContent).toBe(
      "No invoice number",
    );
    unmount();
    renderModal();
    expect(screen.getByTestId("candidate-identity").textContent).toBe(
      "No invoice number",
    );
  });

  it("states what confirming does, verbatim, before the confirm button", () => {
    renderModal();
    const text =
      "Confirming records this payment on the practice, the invoice and the cashout. It cannot be undone from this page.";
    const statement = screen.getByText(
      (_, el) =>
        el?.tagName === "P" && el.textContent?.replace(/\s+/g, " ") === text,
    );
    const button = screen.getByRole("button", { name: /^Confirm / });
    expect(
      statement.compareDocumentPosition(button) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("puts the invoice number in the success toast when present, else keeps the old text", async () => {
    vi.mocked(accountingApi.confirm).mockResolvedValue({} as never);
    const { unmount } = renderModal(full);
    fireEvent.click(screen.getByRole("button", { name: /^Confirm / }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "Payment confirmed",
        "INV-000000-00001 · CONTOH PT marked paid",
      ),
    );
    unmount();
    toast.success.mockClear();
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: /^Confirm / }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "Payment confirmed",
        "CONTOH PT marked paid",
      ),
    );
  });
});

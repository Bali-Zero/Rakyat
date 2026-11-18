/**
 * The confirm dialog must paint an opaque panel with colours this app's theme
 * defines. All names and amounts are invented.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect } from "vitest";
import { ConfirmModal } from "../_components/ConfirmModal";
import { candidate } from "./fixtures";

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ error: vi.fn(), success: vi.fn() }),
}));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: { confirm: vi.fn() },
}));

const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");

function renderModal() {
  return render(
    <ConfirmModal
      txn={{
        id: 1,
        amount_idr: 500000,
        description: "TRF CONTOH",
        txn_date: "2026-01-15",
      }}
      candidate={candidate()}
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

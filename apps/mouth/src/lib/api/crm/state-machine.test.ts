/**
 * Snapshot tests for the frontend mirror of the backend practice state
 * machine. The shape MUST match `VALID_TRANSITIONS` in
 * `apps/backend-rag/backend/services/crm/practice_state_machine.py`.
 *
 * If the backend changes its allowed transitions, these tests fail and
 * the developer must update both files together. This is the cheap
 * drift-detection mechanism that lets us avoid a backend round-trip
 * just to render a dropdown.
 */
import { describe, expect, it } from "vitest";

import {
  ADMIN_ONLY_TRANSITIONS,
  STATUS_LABELS,
  VALID_TRANSITIONS,
  getAllowedNextStatuses,
  type PracticeStatus,
  OPEN_INQUIRY_TYPE_CODE,
  QUOTED_PRICE_REQUIRED_DETAIL,
  QUOTED_PRICE_REQUIRED_TEXT_ID,
  hasQuotedPrice,
  isQuotedPriceRefusal,
  needsQuotedPriceBefore,
  needsServiceBefore,
} from "./state-machine";

describe("VALID_TRANSITIONS — backend snapshot", () => {
  it("has exactly 6 states", () => {
    expect(Object.keys(VALID_TRANSITIONS).sort()).toEqual([
      "cancelled",
      "completed",
      "inquiry",
      "on_process",
      "sending_invoice",
      "waiting_documents",
    ]);
  });

  it("has the canonical forward-progress chain", () => {
    expect(VALID_TRANSITIONS.inquiry).toContain("waiting_documents");
    expect(VALID_TRANSITIONS.waiting_documents).toContain("sending_invoice");
    expect(VALID_TRANSITIONS.sending_invoice).toContain("on_process");
    expect(VALID_TRANSITIONS.on_process).toContain("completed");
  });

  it("allows cancellation from every active state", () => {
    expect(VALID_TRANSITIONS.inquiry).toContain("cancelled");
    expect(VALID_TRANSITIONS.waiting_documents).toContain("cancelled");
    expect(VALID_TRANSITIONS.sending_invoice).toContain("cancelled");
    expect(VALID_TRANSITIONS.on_process).toContain("cancelled");
    expect(VALID_TRANSITIONS.completed).toContain("cancelled");
  });

  it("allows reopening a cancelled practice", () => {
    expect(VALID_TRANSITIONS.cancelled).toEqual(["inquiry"]);
  });

  it("rejects skipping the canonical chain", () => {
    // The original bug: inquiry → on_process should NOT be allowed.
    expect(VALID_TRANSITIONS.inquiry).not.toContain("on_process");
    expect(VALID_TRANSITIONS.inquiry).not.toContain("sending_invoice");
    expect(VALID_TRANSITIONS.inquiry).not.toContain("completed");
  });

  it("matches the exact backend table (drift detector)", () => {
    // Byte-for-byte mirror of practice_state_machine.py:20-26
    expect(VALID_TRANSITIONS).toEqual({
      inquiry: ["waiting_documents", "cancelled"],
      waiting_documents: ["sending_invoice", "inquiry", "cancelled"],
      sending_invoice: ["on_process", "waiting_documents", "cancelled"],
      on_process: ["completed", "sending_invoice", "cancelled"],
      completed: ["cancelled"],
      cancelled: ["inquiry"],
    });
  });
});

describe("ADMIN_ONLY_TRANSITIONS — backend snapshot", () => {
  it("matches the backend list", () => {
    expect(ADMIN_ONLY_TRANSITIONS).toEqual([
      ["completed", "cancelled"],
      ["cancelled", "inquiry"],
    ]);
  });
});

describe("STATUS_LABELS", () => {
  it("provides a label for every state", () => {
    for (const state of Object.keys(VALID_TRANSITIONS) as PracticeStatus[]) {
      expect(STATUS_LABELS[state]).toBeTruthy();
    }
  });
});

describe("getAllowedNextStatuses", () => {
  it("includes the current state so the dropdown keeps its selection", () => {
    expect(getAllowedNextStatuses("inquiry")).toContain("inquiry");
    expect(getAllowedNextStatuses("on_process")).toContain("on_process");
  });

  it("returns only legal forward + cancel from inquiry (the original bug case)", () => {
    expect(getAllowedNextStatuses("inquiry").sort()).toEqual([
      "cancelled",
      "inquiry",
      "waiting_documents",
    ]);
  });

  it("hides admin-only transitions for non-admins", () => {
    // From completed, the only allowed transition is to cancelled, which
    // is admin-only — so a non-admin sees ONLY the current state.
    expect(getAllowedNextStatuses("completed", false)).toEqual(["completed"]);
    // From cancelled, the only allowed transition is back to inquiry,
    // also admin-only — non-admins see only the current state.
    expect(getAllowedNextStatuses("cancelled", false)).toEqual(["cancelled"]);
  });

  it("exposes admin-only transitions to admins", () => {
    expect(getAllowedNextStatuses("completed", true)).toEqual([
      "completed",
      "cancelled",
    ]);
    expect(getAllowedNextStatuses("cancelled", true)).toEqual([
      "cancelled",
      "inquiry",
    ]);
  });

  it("does NOT include illegal jumps (regression test for kita.balizero.com bug)", () => {
    const fromInquiry = getAllowedNextStatuses("inquiry");
    expect(fromInquiry).not.toContain("on_process");
    expect(fromInquiry).not.toContain("sending_invoice");
    expect(fromInquiry).not.toContain("completed");
  });

  it("falls back to current-only on unknown / legacy states", () => {
    // The backend has a legacy state map (in_progress → on_process). The
    // frontend may receive a not-yet-normalized value from old data; we
    // keep the current value selectable but offer no transitions until
    // it is normalized.
    expect(getAllowedNextStatuses("in_progress" as PracticeStatus)).toEqual([
      "in_progress",
    ]);
    expect(getAllowedNextStatuses("payment_pending" as PracticeStatus)).toEqual(
      ["payment_pending"],
    );
  });
});

describe("needsServiceBefore — open inquiry gate (backend mirror)", () => {
  it("blocks every state past inquiry while the placeholder is set", () => {
    for (const target of [
      "waiting_documents",
      "sending_invoice",
      "on_process",
      "completed",
    ]) {
      expect(needsServiceBefore(target, OPEN_INQUIRY_TYPE_CODE)).toBe(true);
    }
  });

  it("lets the placeholder stay in inquiry or be cancelled", () => {
    expect(needsServiceBefore("inquiry", OPEN_INQUIRY_TYPE_CODE)).toBe(false);
    expect(needsServiceBefore("cancelled", OPEN_INQUIRY_TYPE_CODE)).toBe(false);
  });

  it("never blocks a real or unknown service", () => {
    expect(
      needsServiceBefore("waiting_documents", "kitas_working_onshore"),
    ).toBe(false);
    expect(needsServiceBefore("waiting_documents", undefined)).toBe(false);
    expect(needsServiceBefore("waiting_documents", null)).toBe(false);
  });
});

describe("Quoted Price gate for Sending Invoice (backend mirror)", () => {
  it("pins the backend detail and the Indonesian text", () => {
    expect(QUOTED_PRICE_REQUIRED_DETAIL).toBe(
      "Quoted Price is required before Sending Invoice",
    );
    expect(QUOTED_PRICE_REQUIRED_TEXT_ID).toBe(
      "Isi Quoted Price dulu sebelum Sending Invoice",
    );
  });

  it("hasQuotedPrice: only a number above zero counts", () => {
    for (const bad of [
      null,
      undefined,
      0,
      "0",
      "",
      "  ",
      "abc",
      NaN,
      -5,
      true,
    ]) {
      expect(hasQuotedPrice(bad)).toBe(false);
    }
    for (const good of [1, 0.5, "1500000", "2.5", 1_500_000]) {
      expect(hasQuotedPrice(good)).toBe(true);
    }
  });

  it("normalises legacy names: a priceless legacy sending-invoice practice is not blocked, a legacy target is", () => {
    for (const legacy of [
      "quotation_sent",
      "payment_pending",
      "waiting_payment",
    ]) {
      expect(needsQuotedPriceBefore("sending_invoice", null, legacy)).toBe(
        false,
      );
      expect(needsQuotedPriceBefore(legacy, null, "waiting_documents")).toBe(
        true,
      );
      expect(needsQuotedPriceBefore(legacy, 5, "waiting_documents")).toBe(
        false,
      );
    }
    expect(
      needsQuotedPriceBefore("in_progress", null, "waiting_documents"),
    ).toBe(false);
  });

  it("blocks entering sending_invoice without a price and nothing else", () => {
    expect(
      needsQuotedPriceBefore("sending_invoice", null, "waiting_documents"),
    ).toBe(true);
    expect(needsQuotedPriceBefore("sending_invoice", 0, "on_process")).toBe(
      true,
    );
    expect(
      needsQuotedPriceBefore("sending_invoice", 1000, "waiting_documents"),
    ).toBe(false);
    for (const target of [
      "waiting_documents",
      "on_process",
      "completed",
      "cancelled",
      "inquiry",
    ]) {
      expect(needsQuotedPriceBefore(target, null, "inquiry")).toBe(false);
    }
  });

  it("never blocks a practice that is already in sending_invoice", () => {
    expect(
      needsQuotedPriceBefore("sending_invoice", null, "sending_invoice"),
    ).toBe(false);
  });

  it("recognises the backend refusal by detail or message, and nothing else", () => {
    expect(isQuotedPriceRefusal({ detail: QUOTED_PRICE_REQUIRED_DETAIL })).toBe(
      true,
    );
    expect(isQuotedPriceRefusal(new Error(QUOTED_PRICE_REQUIRED_DETAIL))).toBe(
      true,
    );
    expect(isQuotedPriceRefusal(new Error("Invalid status transition"))).toBe(
      false,
    );
    expect(isQuotedPriceRefusal(null)).toBe(false);
    expect(
      isQuotedPriceRefusal("Quoted Price is required before Sending Invoice"),
    ).toBe(false);
  });
});

/**
 * Vertical 1 (Visa & Visa Oracle) API Contract Parity Tests — Phase 2 Fixes.
 *
 * Covers:
 * - VISA-3C-01: `/visa/clock` select options belong strictly to backend `VisaType`
 *   (retired `"B211A"` removed, `"C1"` present, no duplicate option keys) and
 *   `<input type="date">` enforces `min`/`max` matching `ClockRequest._entry_date_reasonable`.
 * - VISA-3A-01: `/visa/clock/[hash]` `<AppEmailOptIn>` contract aligns with
 *   `POST /api/visa/clock/email` (`app: "visa_clock"`, `result_hash`, `payload`).
 * - VISA-3C-02 + VISA-3D-01: `POST /api/assessment/submit` translates backend
 *   HTTP 200 `{ success: false }` into HTTP 502 `{ success: false, error }`, and
 *   `assessment/page.tsx` verifies `res.ok` + `result?.success !== false` (including `submitBonus`).
 * - VISA-3C-03: GARUDA VOA staff `[practiceId]` rotates `Idempotency-Key` when
 *   transition form fields or assignment target change after a failed attempt,
 *   while preserving key reuse on an unchanged retry.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const navMocks = vi.hoisted(() => ({
  push: vi.fn(),
  useParams: vi.fn(),
}));

const apiMocks = vi.hoisted(() => ({
  getProfile: vi.fn(),
  isAdmin: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

const garudaMocks = vi.hoisted(() => ({
  getStaffPractice: vi.fn(),
  transitionPractice: vi.fn(),
  assignPractice: vi.fn(),
  useGarudaAssignmentTargets: vi.fn(),
}));

const trackerMocks = vi.hoisted(() => ({
  viewed: vi.fn(),
  formStarted: vi.fn(),
  formSubmitted: vi.fn(),
  formSubmitFailed: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useParams: navMocks.useParams,
  useRouter: () => ({ push: navMocks.push }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getProfile: apiMocks.getProfile,
    isAdmin: apiMocks.isAdmin,
  },
}));

vi.mock("@/components/ui/toast", () => ({
  useToast: () => toastMocks,
}));

vi.mock("@/lib/logger", () => ({
  logger: {
    error: vi.fn(),
    warn: vi.fn(),
    info: vi.fn(),
    debug: vi.fn(),
  },
}));

vi.mock("@balizero/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@balizero/core")>();
  return {
    ...actual,
    useFunnelApp: () => trackerMocks,
  };
});

vi.mock("@/app/(workspace)/garuda-voa/assignment-targets", () => ({
  useGarudaAssignmentTargets: garudaMocks.useGarudaAssignmentTargets,
}));

vi.mock("@/app/(workspace)/garuda-voa/api-client", async () => {
  const actual = await vi.importActual<
    typeof import("@/app/(workspace)/garuda-voa/api-client")
  >("@/app/(workspace)/garuda-voa/api-client");
  return {
    ...actual,
    getStaffPractice: garudaMocks.getStaffPractice,
    transitionPractice: garudaMocks.transitionPractice,
    assignPractice: garudaMocks.assignPractice,
  };
});

import VisaClockPage from "@/app/visa/clock/page";
import { POST as submitAssessment } from "@/app/api/assessment/submit/route";
import GarudaVoaStaffDetailPage from "@/app/(workspace)/garuda-voa/[practiceId]/page";
import { GarudaStaffError } from "@/app/(workspace)/garuda-voa/api-client";

const SRC_ROOT = resolve(__dirname, "../../..");

const BACKEND_VISA_TYPES = new Set([
  "A1",
  "B1",
  "C1",
  "C2",
  "C6",
  "C7",
  "C7A",
  "C7B",
  "C18",
  "C22A",
  "D2",
  "D12",
  "E23",
  "E23-FREELANCE",
  "E28A",
  "E30A",
  "E31",
  "E33",
  "E33E",
  "E33F",
  "E33G",
]);

function localIsoDateOffset(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}

describe("Swarm Vertical 1 — Visa & Visa Oracle Contract Parity", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.unstubAllEnvs();
  });

  describe("VISA-3C-01: /visa/clock VisaType enum & entry_date bounds", () => {
    it("offers only valid backend VisaType codes (no retired B211A, C1 present, unique codes)", () => {
      render(createElement(VisaClockPage));
      const select = screen.getByLabelText("Visa type") as HTMLSelectElement;
      const optionValues = Array.from(select.options)
        .map((opt) => opt.value)
        .filter((val) => val.length > 0);

      expect(optionValues).not.toContain("B211A");
      expect(optionValues).toContain("C1");
      expect(new Set(optionValues).size).toBe(optionValues.length);
      for (const code of optionValues) {
        expect(BACKEND_VISA_TYPES.has(code)).toBe(true);
      }
    });

    it("constrains entry_date input min/max to [today - 2190d, today + 31d]", () => {
      render(createElement(VisaClockPage));
      const dateInput = screen.getByLabelText("Entry date") as HTMLInputElement;
      expect(dateInput.min).toBe(localIsoDateOffset(-365 * 6));
      expect(dateInput.max).toBe(localIsoDateOffset(31));
    });
  });

  describe("VISA-3A-01: /visa/clock/[hash] -> POST /api/visa/clock/email contract", () => {
    it("wires AppEmailOptIn to /api/visa/clock/email with app='visa_clock' and timeline payload", () => {
      const clockResultSrc = readFileSync(
        resolve(SRC_ROOT, "app/visa/clock/[hash]/page.tsx"),
        "utf-8",
      );
      expect(clockResultSrc).toContain('endpoint="/api/visa/clock/email"');
      expect(clockResultSrc).toContain('app="visa_clock"');
      expect(clockResultSrc).toContain("resultHash={data.hash}");
      expect(clockResultSrc).toContain("visa_type: data.visa_type");
      expect(clockResultSrc).toContain("entry_date: data.entry_date");
      expect(clockResultSrc).toContain("expiry_date: data.expiry_date");
    });
  });

  describe("VISA-3C-02 + VISA-3D-01: /api/assessment/submit & UI callers", () => {
    const routeSrc = readFileSync(
      resolve(SRC_ROOT, "app/api/assessment/submit/route.ts"),
      "utf-8",
    );
    const allowedRecipientMatch = /payload\.to !== "([^"]+)"/.exec(routeSrc);
    const allowedRecipient = allowedRecipientMatch?.[1] ?? "";

    it("returns HTTP 502 { success: false, error } when backend responds HTTP 200 with success: false", async () => {
      vi.stubEnv("NUZANTARA_API_URL", "https://backend.invalid");
      vi.stubEnv("API_KEY", "test-relay-token"); // pragma: allowlist secret

      global.fetch = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            success: false,
            message: "All providers failed: upstream unavailable",
            provider: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );

      const req = new NextRequest("http://localhost/api/assessment/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          to: allowedRecipient,
          subject: "Assessment Block 1",
          body: "<p>Answer</p>",
        }),
      });

      const res = await submitAssessment(req);
      expect(res.status).toBe(502);
      const json = (await res.json()) as { success: boolean; error: string };
      expect(json.success).toBe(false);
      expect(json.error).toContain("All providers failed");
    });

    it("passes through HTTP 200 when backend confirms success: true", async () => {
      vi.stubEnv("NUZANTARA_API_URL", "https://backend.invalid");
      vi.stubEnv("API_KEY", "test-relay-token"); // pragma: allowlist secret

      global.fetch = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            success: true,
            message: "Sent",
            provider: "resend",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );

      const req = new NextRequest("http://localhost/api/assessment/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          to: allowedRecipient,
          subject: "Assessment Block 1",
          body: "<p>Answer</p>",
        }),
      });

      const res = await submitAssessment(req);
      expect(res.status).toBe(200);
      const json = (await res.json()) as { success: boolean };
      expect(json.success).toBe(true);
    });

    it("guards assessment/page.tsx (blocks + submitBonus) on both res.ok and result.success", () => {
      const assessmentSrc = readFileSync(
        resolve(SRC_ROOT, "app/(assessment)/assessment/page.tsx"),
        "utf-8",
      );

      // Both block submit and submitBonus check `if (!res.ok)` and `result?.success === false`
      const okChecks = assessmentSrc.match(/if \(!res\.ok\)/g) ?? [];
      const successChecks =
        assessmentSrc.match(/if \(result\?\.success === false\)/g) ?? [];
      expect(okChecks.length).toBeGreaterThanOrEqual(2);
      expect(successChecks.length).toBeGreaterThanOrEqual(2);
    });
  });

  describe("VISA-3C-03: GARUDA VOA staff Idempotency-Key rotation", () => {
    const PRACTICE = {
      practice_id: "prc_0000000000000001",
      order_id: "ord_0000000000000001",
      state: "Received" as const,
      assigned_to: null,
      updated_at: "2026-11-01T10:00:00Z",
      artifact_available: false,
      private_staff_note: null,
      resume_target: null,
      active_block_id: null,
    };

    beforeEach(() => {
      navMocks.useParams.mockReturnValue({
        practiceId: "prc_0000000000000001",
      });
      apiMocks.getProfile.mockResolvedValue({ role: "admin" });
      apiMocks.isAdmin.mockReturnValue(true);
      garudaMocks.getStaffPractice.mockResolvedValue(PRACTICE);
      garudaMocks.useGarudaAssignmentTargets.mockReturnValue({
        data: [
          { email: "staff_target_a", label: "Staff A" },
          { email: "staff_target_b", label: "Staff B" },
        ],
        isError: false,
      });
    });

    it("reuses Idempotency-Key on identical transition retry, and rotates it when a form field is edited", async () => {
      garudaMocks.transitionPractice
        .mockRejectedValueOnce(new GarudaStaffError("INVALID_REQUEST", false, 422))
        .mockRejectedValueOnce(new GarudaStaffError("INVALID_REQUEST", false, 422))
        .mockResolvedValueOnce({
          practice: {
            practice_id: "prc_0000000000000001",
            state: "Blocked",
            artifact_available: false,
          },
          replayed: false,
        });

      render(createElement(GarudaVoaStaffDetailPage));

      await waitFor(() =>
        expect(screen.getByTestId("transition-PR-03")).toBeTruthy(),
      );
      fireEvent.click(screen.getByTestId("transition-PR-03"));

      fireEvent.change(screen.getByLabelText("Customer reason key"), {
        target: { value: "bad_reason" },
      });
      fireEvent.change(screen.getByLabelText("Required action key"), {
        target: { value: "garuda_voa.action.upload_document" },
      });

      // Attempt 1 (fails 422)
      fireEvent.click(screen.getByText("Apply"));
      await waitFor(() =>
        expect(garudaMocks.transitionPractice).toHaveBeenCalledTimes(1),
      );
      const keyAttempt1 =
        garudaMocks.transitionPractice.mock.calls[0][0].idempotencyKey;

      // Attempt 2 with UNCHANGED fields -> must reuse same key
      fireEvent.click(screen.getByText("Apply"));
      await waitFor(() =>
        expect(garudaMocks.transitionPractice).toHaveBeenCalledTimes(2),
      );
      const keyAttempt2 =
        garudaMocks.transitionPractice.mock.calls[1][0].idempotencyKey;
      expect(keyAttempt2).toBe(keyAttempt1);

      // Attempt 3 after fixing the field -> must rotate to a fresh key
      fireEvent.change(screen.getByLabelText("Customer reason key"), {
        target: { value: "garuda_voa.practice.missing_document" },
      });
      fireEvent.click(screen.getByText("Apply"));
      await waitFor(() =>
        expect(garudaMocks.transitionPractice).toHaveBeenCalledTimes(3),
      );
      const keyAttempt3 =
        garudaMocks.transitionPractice.mock.calls[2][0].idempotencyKey;
      expect(keyAttempt3).not.toBe(keyAttempt1);
    });

    it("rotates assignmentIdempotencyKey when switching assignee target after a failure", async () => {
      garudaMocks.assignPractice
        .mockRejectedValueOnce(new GarudaStaffError("INVALID_REQUEST", false, 422))
        .mockResolvedValueOnce({
          ...PRACTICE,
          assigned_to: "staff_target_b",
        });

      render(createElement(GarudaVoaStaffDetailPage));

      const select = (await screen.findByLabelText(
        "Assigned to",
      )) as HTMLSelectElement;

      // Attempt 1: pick staff_target_a -> fails 422
      fireEvent.change(select, { target: { value: "staff_target_a" } });
      await waitFor(() =>
        expect(garudaMocks.assignPractice).toHaveBeenCalledTimes(1),
      );
      const keyTargetA =
        garudaMocks.assignPractice.mock.calls[0][0].idempotencyKey;

      // Attempt 2: pick staff_target_b -> different command, must mint a fresh key
      fireEvent.change(select, { target: { value: "staff_target_b" } });
      await waitFor(() =>
        expect(garudaMocks.assignPractice).toHaveBeenCalledTimes(2),
      );
      const keyTargetB =
        garudaMocks.assignPractice.mock.calls[1][0].idempotencyKey;
      expect(keyTargetB).not.toBe(keyTargetA);
    });
  });
});

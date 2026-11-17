import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthApi } from "../../auth/auth.api";
import { ApiClientBase } from "../../client";
import { ApiError } from "../../error-handler";
import { PortalApi } from "../portal.api";
import type {
  LKPMBatchItem,
  LKPMDraft,
  LKPMDraftSummary,
  LKPMReadyPack,
  ProcessTimeline,
} from "../portal.types";
import { ProcessTimelineData } from "../../../schemas/process";

const SRC_ROOT = resolve(__dirname, "../../../..");

function makeApi(response: unknown) {
  const request = vi.fn().mockResolvedValue(response);
  const api = new PortalApi({ request } as unknown as ApiClientBase);
  return { api, request };
}

describe("swarm portal-auth contracts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("PORTAL-3C-01: validateInviteToken & completeRegistration normalize snake_case and camelCase", async () => {
    const { api } = makeApi({
      valid: true,
      client_name: "Synthetic Client",
      invitation_id: 7,
      client_id: 42,
    });
    const out = await api.validateInviteToken("invite-abc");
    expect(out).toEqual({
      valid: true,
      clientName: "Synthetic Client",
      invitationId: 7,
      clientId: 42,
    });

    const camel = makeApi({
      valid: true,
      clientName: "Camel Client",
      invitationId: 8,
      clientId: 43,
    });
    expect(await camel.api.validateInviteToken("invite-camel")).toEqual({
      valid: true,
      clientName: "Camel Client",
      invitationId: 8,
      clientId: 43,
    });

    const reg = makeApi({
      success: true,
      message: "ok",
      user_id: 9,
      redirect_to: "/portal/login-upgraded",
    });
    const regOut = await reg.api.completeRegistration({
      invitationId: 7,
    } as never);
    expect(regOut.redirectTo).toBe("/portal/login-upgraded");
    expect(regOut.userId).toBe("9");
  });

  it("PORTAL-3C-02: getVisaStatus preserves history period and completed status", async () => {
    const { api } = makeApi({
      data: {
        current: null,
        history: [
          {
            id: 1,
            visa_type: "KITAS",
            period: "2024 - 2025",
            status: "completed",
          },
          {
            id: 2,
            visa_type: "VOA",
            issue_date: "2023-01-01",
            expiry_date: "2023-03-01",
            status: "expired",
          },
        ],
      },
    });
    const out = await api.getVisaStatus();
    expect(out.history[0].period).toBe("2024 - 2025");
    expect(out.history[0].status).toBe("completed");
    expect(out.history[1].period).toBe("2023-01-01 — 2023-03-01");
    expect(out.history[1].status).toBe("expired");
  });

  it("PORTAL-3C-03: process schema accepts under_review, rejected, nullable fields, and ignores prototype keys", () => {
    const base = { practice_id: 1, steps: [] };
    const nullStatus = ProcessTimelineData.safeParse({
      ...base,
      practice_name: null,
      practice_category: null,
      current_status: null,
    });
    expect(nullStatus.success).toBe(true);

    const protoCheck = ProcessTimelineData.safeParse({
      ...base,
      current_status: "constructor",
    });
    expect(protoCheck.success).toBe(false);

    const parsed = ProcessTimelineData.parse({
      practice_id: 2,
      practice_name: null,
      practice_category: null,
      current_status: "under_review",
      steps: [
        {
          status: "under_review",
          label: "Under Review",
          completed: false,
          is_current: true,
          changed_at: null,
        },
        {
          status: "rejected",
          label: "Rejected",
          completed: false,
          is_current: false,
          changed_at: null,
        },
      ],
    });
    expect(parsed.current_status).toBe("on_process");
    expect(parsed.steps[0].status).toBe("on_process");
    expect(parsed.steps[1].status).toBe("cancelled");

    const tsTimeline: ProcessTimeline = {
      practice_id: 2,
      practice_name: null,
      practice_category: null,
      current_status: null,
      start_date: null,
      completion_date: null,
      expiry_date: null,
      steps: [],
    };
    expect(tsTimeline.current_status).toBeNull();
  });

  it("PORTAL-3D-01: verifyMagicLink passes redirectOnUnauthorized: false", async () => {
    const request = vi.fn().mockResolvedValue({
      success: true,
      message: "ok",
      data: {
        access_token: "opaque",
        token_type: "Bearer",
        expires_in: 3600,
        user: {
          id: "u1",
          email: "",
          name: "Member",
          role: "client",
        },
      },
    });
    const authApi = new AuthApi({
      request,
      setToken: vi.fn(),
      setUserProfile: vi.fn(),
    } as unknown as ApiClientBase);

    await authApi.verifyMagicLink("magic-link-id");
    expect(request).toHaveBeenCalledWith(
      "/api/auth/verify-magic/magic-link-id",
      { method: "GET", redirectOnUnauthorized: false },
      90000,
    );
  });

  it("PORTAL-3C-06: getDocuments & uploadDocument map snake_case fields onto PortalDocument", async () => {
    const { api } = makeApi({
      data: {
        documents: [
          {
            id: 3,
            file_name: "doc.pdf",
            document_type: "tax_report",
            size_kb: 12,
            expiry_date: "2026-01-01",
            created_at: "2025-01-01",
            practice_id: 5,
            downloadable: true,
            status: "verified",
          },
        ],
      },
    });
    const docs = await api.getDocuments();
    expect(docs).toHaveLength(1);
    expect(docs[0]).toMatchObject({
      id: "3",
      name: "doc.pdf",
      type: "tax_report",
      size: "12 KB",
      uploadDate: "2025-01-01",
      expiryDate: "2026-01-01",
      practiceId: 5,
      downloadUrl: "/api/portal/documents/3/download",
      status: "verified",
    });
  });

  it("KBLI-3A-03 / 3C-01 / 3C-02 / 3C-10: LKPM portal client, lkpm/[id] null guards, and nullable types align with backend", async () => {
    const flat = makeApi({
      items: [
        {
          id: 1,
          quarter: "Q3",
          year: 2026,
          status: "draft",
          total_realized: 0,
          updated_at: null,
        },
      ],
    });
    expect(await flat.api.getLKPMHistory()).toHaveLength(1);
    expect(flat.request.mock.calls[0][0]).toBe("/api/v1/lkpm/history/me");

    const wrapped = makeApi({ data: { items: [{ id: 1 }, { id: 2 }] } });
    expect(await wrapped.api.getLKPMHistory(99)).toHaveLength(2);

    const approved = makeApi({ status: "approved" });
    expect(await approved.api.approveLKPMDraft(10)).toEqual({ success: true });

    const readyPackPage = readFileSync(
      resolve(SRC_ROOT, "app/(workspace)/lkpm/[id]/page.tsx"),
      "utf-8",
    );
    expect(readyPackPage).toContain("pack.validation_summary?.is_valid");
    expect(readyPackPage).toContain("pack.validation_summary?.red_count ?? 0");

    const zeroRealization = {
      equipment_domestic: 0,
      equipment_import: 0,
      building_domestic: 0,
      building_import: 0,
      vehicle_domestic: 0,
      vehicle_import: 0,
      land: 0,
      working_capital: 0,
      other: 0,
      total_domestic: 0,
      total_import: 0,
      grand_total: 0,
    };
    const pack: LKPMReadyPack = {
      draft_id: 1,
      company_name: "PT Synthetic",
      quarter: "Q3",
      year: 2026,
      realized: zeroRealization,
      cumulative: zeroRealization,
      employment: { tki: 2, tka: 0, total: 2 },
      validation_summary: null,
      html_content: "<p>ok</p>",
    };
    const draft: Partial<LKPMDraft> = {
      quarterly_revenue: 100,
      annual_revenue: 400,
      narrative_obstacles: null,
      narrative_plans: null,
      validation: { is_valid: true, alerts: [] },
      updated_at: null,
    };
    const batchItem: Partial<LKPMBatchItem> = { updated_at: null };
    const summary: Partial<LKPMDraftSummary> = { updated_at: null };
    expect(pack.validation_summary).toBeNull();
    expect(draft.updated_at).toBeNull();
    expect(batchItem.updated_at).toBeNull();
    expect(summary.updated_at).toBeNull();
  });

  it("DEFECT-3D-02 & PORTAL-3D-07: ApiClientBase formats structured 401 detail and falls back to error.message", async () => {
    const client = new ApiClientBase("https://api.example.invalid");

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { error: "auth_error", message: "Token signature mismatch" },
          }),
          { status: 401, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    await expect(
      client.request("/api/portal/dashboard", {
        method: "GET",
        redirectOnUnauthorized: false,
      }),
    ).rejects.toThrowError(
      expect.objectContaining({
        message: "Token signature mismatch",
        statusCode: 401,
      } satisfies Partial<ApiError>),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { code: "OPAQUE_401" },
          }),
          { status: 401, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    await expect(
      client.request("/api/portal/dashboard", {
        method: "GET",
        redirectOnUnauthorized: false,
      }),
    ).rejects.toThrowError("Session expired. Please login again.");

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ message: "Upstream gateway timeout" }), {
          status: 502,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    await expect(
      client.request("/api/portal/dashboard", { method: "GET" }),
    ).rejects.toThrowError("Upstream gateway timeout");
  });
});

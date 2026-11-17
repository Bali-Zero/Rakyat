import { NextRequest } from "next/server";
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
import { PENDING_COOKIE } from "../../../../app/visa/voa/auth/contract";

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

  it("PORTAL-3A-01: uploadPaymentProof posts multipart form data and unwraps envelope or fallback", async () => {
    const wrapped = makeApi({
      success: true,
      message: "Payment proof uploaded",
      data: {
        id: 901,
        status: "received",
        note: null,
        created_at: "2026-11-16T08:00:00Z",
      },
    });
    const file = new File(["%PDF-1.4"], "receipt.pdf", {
      type: "application/pdf",
    });
    const res = await wrapped.api.uploadPaymentProof(55, file);
    expect(wrapped.request).toHaveBeenCalledWith(
      "/api/portal/billing/55/payment-proof",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
    expect(res).toEqual({
      id: 901,
      status: "received",
      note: null,
      created_at: "2026-11-16T08:00:00Z",
    });

    const minimal = makeApi({
      success: true,
      message: "Payment proof uploaded",
    });
    const fallbackRes = await minimal.api.uploadPaymentProof(55, file);
    expect(fallbackRes.id).toBe(55);
    expect(fallbackRes.status).toBe("received");
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

  it("PORTAL-3C-03: process schema accepts under_review, rejected, and nullable fields", () => {
    const base = { practice_id: 1, steps: [] };
    const nullStatus = ProcessTimelineData.safeParse({
      ...base,
      practice_name: null,
      practice_category: null,
      current_status: null,
    });
    expect(nullStatus.success).toBe(true);

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

  it("PORTAL-3D-04: VOA magic exchange preserves pending cookie on 429/5xx/network error and clears on 401", async () => {
    const prevFlag = process.env.GARUDA_PUBLIC_ENABLED;
    process.env.GARUDA_PUBLIC_ENABLED = "true";
    try {
      const pendingVal = `${"R".repeat(24)}.${"t".repeat(40)}`;
      const makeReq = () =>
        new NextRequest("https://balizero.com/visa/voa/auth/exchange", {
          method: "POST",
          headers: {
            "content-type": "application/x-www-form-urlencoded",
            cookie: `${PENDING_COOKIE}=${pendingVal}`,
            "sec-fetch-site": "same-origin",
          },
        });
      const hasClear = (res: Response) =>
        res.headers
          .getSetCookie()
          .some(
            (c) => c.startsWith(`${PENDING_COOKIE}=`) && c.includes("Max-Age=0"),
          );

      const { POST } = await import("../../../../app/visa/voa/auth/exchange/route");

      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response(null, { status: 503 })),
      );
      expect(hasClear(await POST(makeReq()))).toBe(false);

      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response(null, { status: 429 })),
      );
      expect(hasClear(await POST(makeReq()))).toBe(false);

      vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNRESET")));
      expect(hasClear(await POST(makeReq()))).toBe(false);

      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response(null, { status: 401 })),
      );
      expect(hasClear(await POST(makeReq()))).toBe(true);
    } finally {
      if (prevFlag === undefined) delete process.env.GARUDA_PUBLIC_ENABLED;
      else process.env.GARUDA_PUBLIC_ENABLED = prevFlag;
    }
  });

  it("KBLI-3A-03 / 3C-01 / 3C-02 / 3C-10: LKPM portal client and nullable types align with backend", async () => {
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

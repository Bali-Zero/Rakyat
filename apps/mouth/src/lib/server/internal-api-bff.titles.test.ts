import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { NextRequest } from "next/server";
import { POST as approvePost } from "@/app/api/intel/staging/approve/[type]/[id]/route";
import {
  EDITORIAL_APPROVER_ROLES,
  authorizeInternalBffRequest,
} from "@/lib/server/internal-api-bff";

// Reorganisation 2026-11-09: four job titles replace four older ones, and the
// editorial-approve route is strictly role-keyed (the internal-domain branch
// admits staff to the BFF but not to this route).
const NEW_TITLES = [
  "Direktur",
  "Tax Supervisor",
  "Tax Responsible",
  "Senior Consultant",
  // owner order 2026-11-15: same access as Tax Lead
  "Tax Consultant",
];
const OLD_TITLES_STILL_HELD = [
  "Supervisor",
  "Tax Lead",
  "Team Leader",
  "Executive Consultant",
];
const NOT_EDITORIAL = ["HR & Accounting", "Accounting", "Reception"];

describe("editorial approver roles carry the 2026-11-09 titles", () => {
  const mockFetch = vi.fn();
  const originalEnv = process.env;

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset();
    global.fetch = mockFetch;
    process.env = {
      ...originalEnv,
      NUZANTARA_API_URL: "https://nuzantara-rag.fly.dev",
      NUZANTARA_INTERNAL_API_KEY: "internal-secret-key", // pragma: allowlist secret
    };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  async function approveAs(role: string) {
    mockFetch
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ data: { email: "staff@balizero.com", role } }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    const req = new Request(
      "http://localhost/api/intel/staging/approve/visa/item-1",
      {
        method: "POST",
        headers: { Authorization: "Bearer staff-jwt" },
        body: JSON.stringify({}),
      },
    ) as unknown as NextRequest;
    return approvePost(req, {
      params: Promise.resolve({ type: "visa", id: "item-1" }),
    });
  }

  it.each([...NEW_TITLES, ...OLD_TITLES_STILL_HELD])(
    "innocence: %s may approve",
    async (role) => {
      const res = await approveAs(role);
      expect(res.status).toBe(200);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    },
  );

  it.each(NOT_EDITORIAL)(
    "guilt: %s is still refused and the key is never forwarded",
    async (role) => {
      const res = await approveAs(role);
      expect(res.status).toBe(403);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    },
  );

  it("guilt: the set holds exactly the new titles it is meant to add", () => {
    for (const title of NEW_TITLES) {
      expect(EDITORIAL_APPROVER_ROLES.has(title.toLowerCase())).toBe(true);
    }
    for (const title of NOT_EDITORIAL) {
      expect(EDITORIAL_APPROVER_ROLES.has(title.toLowerCase())).toBe(false);
    }
  });

  // The base staff gate: a profile with an EXTERNAL email and no
  // is_team_member flag is admitted by role alone, so every new title must be
  // admitted exactly like the title it replaces.
  const TITLE_PAIRS: Array<[string, string]> = [
    ["Supervisor", "Direktur"],
    ["Tax Lead", "Tax Supervisor"],
    ["Team Leader", "Tax Responsible"],
    ["Executive Consultant", "Senior Consultant"],
    ["Accounting", "HR & Accounting"],
    // owner order 2026-11-15
    ["Tax Lead", "Tax Consultant"],
  ];

  async function baseGateAdmits(role: string): Promise<boolean> {
    mockFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ data: { email: "someone@example.org", role } }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const req = new Request("http://localhost/api/audio/speech", {
      method: "POST",
      headers: { Authorization: "Bearer staff-jwt" },
    });
    const out = await authorizeInternalBffRequest(req);
    return !(out instanceof Response);
  }

  it.each(TITLE_PAIRS)(
    "base gate, external email: %s and its replacement %s are admitted alike",
    async (oldTitle, newTitle) => {
      expect(await baseGateAdmits(oldTitle)).toBe(true);
      expect(await baseGateAdmits(newTitle)).toBe(true);
    },
  );

  it("base gate guilt control: an unknown title with an external email is refused", async () => {
    expect(await baseGateAdmits("Visitor Liaison")).toBe(false);
  });
});

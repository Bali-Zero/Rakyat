import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: {
    debug: vi.fn(),
  },
}));

import { logger } from "@/lib/logger";
import {
  logRouteTouch,
  resolveRouteTouch,
  sanitizeRouteSegment,
} from "./route-touch";

describe("route-touch PII-safe telemetry", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("redacts emails, phones, UUIDs, numeric IDs, long tokens, name slugs, short tokens, and dotted IDs", () => {
    expect(sanitizeRouteSegment("client@example.com")).toBe(":email");
    expect(sanitizeRouteSegment("client%40example.com")).toBe(":email");
    expect(sanitizeRouteSegment("+6284000220000")).toBe(":phone");
    expect(sanitizeRouteSegment("6284000220000")).toBe(":id");
    expect(sanitizeRouteSegment("4821")).toBe(":id");
    expect(sanitizeRouteSegment("550e8400-e29b-41d4-a716-446655440000")).toBe(
      ":uuid",
    );
    expect(sanitizeRouteSegment("1a2B3c4D5e6F7g8H9i0J1k2L3m4N")).toBe(":token");
    expect(sanitizeRouteSegment("mario-rossi")).toBe(":param");
    expect(sanitizeRouteSegment("Mario%20Rossi")).toBe(":param");
    expect(sanitizeRouteSegment("tok123")).toBe(":param");
    expect(sanitizeRouteSegment("wamid.123")).toBe(":param");
    expect(sanitizeRouteSegment("conversations")).toBe("conversations");
  });

  it("resolves route templates without leaking raw PII or query strings", () => {
    const touch = resolveRouteTouch(
      "/api/whatsapp/messages/+6284000220000?limit=50&email=secret@example.com",
      "get",
    );

    expect(touch).toEqual({
      family: "whatsapp",
      operation: "messages/:phone",
      routeTemplate: "/api/whatsapp/messages/:phone",
      method: "GET",
    });
  });

  it("logs sanitized routeTemplate, status, and durationMs without raw path", () => {
    logRouteTouch(
      "proxy",
      "/api/crm/clients/client@example.com?phone=+6284000220000",
      "PATCH",
      { status: 200, durationMs: 42 },
    );

    expect(logger.debug).toHaveBeenCalledWith("api.route.touch", {
      component: "RouteInventory",
      action: "route.touch",
      metadata: {
        surface: "proxy",
        invFamily: "crm",
        invOp: "clients/:email",
        routeTemplate: "/api/crm/clients/:email",
        method: "PATCH",
        status: 200,
        durationMs: 42,
      },
    });

    const serialized = JSON.stringify(vi.mocked(logger.debug).mock.calls);
    expect(serialized).not.toContain("client@example.com");
    expect(serialized).not.toContain("+6284000220000");
  });
});

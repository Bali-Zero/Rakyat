import { logger } from "@/lib/logger";

export type RouteTouchSurface = "client" | "proxy" | "handler";

export interface RouteTouch {
  family: string;
  operation: string;
  routeTemplate: string;
  method: string;
}

export interface RouteTouchMetrics {
  status?: number;
  durationMs?: number;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NUMERIC_ID_RE = /^\d+$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PHONE_RE = /^\+?\d[\d\s\-()]{6,}$/;
const HEX_OR_TOKEN_RE = /^[A-Za-z0-9_-]{20,}$/;

/**
 * Explicit allowlist of static route path segments.
 * Any segment NOT in this allowlist is treated as a dynamic parameter and
 * redacted (:email, :phone, :uuid, :id, :token, or :param), guaranteeing that
 * names, slugs, short tokens, dotted message IDs, or unknown user inputs can
 * never leak into logs (Builder Contract 4 / UU PDP).
 */
const KNOWN_STATIC_SEGMENTS = new Set([
  "admin",
  "agentic-rag",
  "alerts",
  "analytics",
  "analyze",
  "approve",
  "articles",
  "assessment",
  "assign",
  "audio",
  "auth",
  "autonomous-execution",
  "bali-zero",
  "blog",
  "book",
  "chains",
  "channels",
  "chat",
  "check",
  "client",
  "clients",
  "clock-in",
  "clock-out",
  "companies",
  "compliance",
  "confirm",
  "conversations",
  "crm",
  "csrf",
  "dashboard",
  "deadlines",
  "density",
  "dlq",
  "documents",
  "download",
  "drive",
  "enqueue",
  "enrichment",
  "evaluate",
  "events",
  "files",
  "folders",
  "gate",
  "gold",
  "health",
  "health-public",
  "history",
  "homepage-hero",
  "ical",
  "image",
  "inbox",
  "indexnow",
  "inspect",
  "intake",
  "intel",
  "intelligence",
  "interactions",
  "kbli",
  "kbli-notebook",
  "knowledge",
  "lab",
  "lkpm",
  "local-audio",
  "login",
  "logout",
  "media",
  "memory",
  "messages",
  "metrics",
  "news",
  "newsletter",
  "notes",
  "notifications",
  "og",
  "omnichannel",
  "pending",
  "permissions",
  "points",
  "portal",
  "portal-challenge",
  "portal-superuser",
  "portfolio",
  "practices",
  "predict",
  "preview",
  "pricing",
  "prime",
  "profile",
  "property",
  "proposal",
  "proxy",
  "publish",
  "purge",
  "qdrant",
  "refresh",
  "regulations",
  "reject",
  "replay",
  "resolve",
  "run-sync",
  "save",
  "search",
  "secondhome",
  "send",
  "sessions",
  "sheets",
  "speech",
  "staging",
  "stats",
  "status",
  "stream",
  "submit",
  "summary",
  "synthesize",
  "tax-calendar",
  "team",
  "temporal",
  "threads",
  "thumbnail",
  "timeline",
  "transcribe",
  "upload",
  "user",
  "users",
  "v1",
  "v2",
  "visa",
  "visa-oracle",
  "visa-oracle-unlock",
  "voice",
  "voice-concierge",
  "wa",
  "web-vitals",
  "webhook",
  "whatsapp",
  "workflow",
  "workspace",
  "workspace-stream",
  "zones",
  "zones-geojson",
  "zoning",
]);

/**
 * Redact any non-static path segment into a type-specific or generic
 * placeholder (:email, :phone, :uuid, :id, :token, :param).
 */
export function sanitizeRouteSegment(rawSegment: string): string {
  let decoded = rawSegment;
  try {
    decoded = decodeURIComponent(rawSegment);
  } catch {
    decoded = rawSegment;
  }
  const trimmed = decoded.trim();
  if (!trimmed) return trimmed;

  const lower = trimmed.toLowerCase();
  if (KNOWN_STATIC_SEGMENTS.has(lower) && trimmed === lower) {
    return lower;
  }

  if (trimmed.includes("@") || EMAIL_RE.test(trimmed)) {
    return ":email";
  }
  if (UUID_RE.test(trimmed)) {
    return ":uuid";
  }
  if (NUMERIC_ID_RE.test(trimmed)) {
    return ":id";
  }
  if (PHONE_RE.test(trimmed)) {
    return ":phone";
  }
  if (
    HEX_OR_TOKEN_RE.test(trimmed) &&
    (/\d/.test(trimmed) || /[A-Z]/.test(trimmed) || trimmed.length >= 28)
  ) {
    return ":token";
  }

  return ":param";
}

export function resolveRouteTouch(path: string, method: string): RouteTouch {
  const normalizedMethod = (method || "GET").toUpperCase();
  const pathOnly = path.split("?")[0] ?? path;
  const rel = pathOnly.replace(/^\/api\/?/, "").replace(/^\/+/, "");
  const rawParts = rel.split("/").filter(Boolean);
  const parts = rawParts.map(sanitizeRouteSegment);

  if (parts.length === 0) {
    return {
      family: "_root",
      operation: normalizedMethod.toLowerCase(),
      routeTemplate: "/api",
      method: normalizedMethod,
    };
  }

  let family: string;
  let opParts: string[];

  if (parts[0] === "bali-zero") {
    family = "bali-zero";
    opParts = parts.slice(1);
  } else if (parts[0] === "agentic-rag") {
    family = "agentic-rag";
    opParts = parts.slice(1);
  } else if (parts[0] === "v1" && parts.length > 1) {
    family = `v1-${parts[1]}`;
    opParts = parts.slice(2);
  } else if (parts[0] === "prime" && parts[1] === "v2") {
    family = "prime-v2";
    opParts = parts.slice(2);
  } else if (parts[0] === "workspace" && parts.length > 1) {
    family = `workspace-${parts[1]}`;
    opParts = parts.slice(2);
  } else {
    family = parts[0].startsWith(":") ? "_dynamic" : parts[0];
    opParts = parts.slice(1);
  }

  const operation =
    opParts.length > 0 ? opParts.join("/") : normalizedMethod.toLowerCase();
  const routeTemplate = `/api/${parts.join("/")}`;

  return {
    family,
    operation,
    routeTemplate,
    method: normalizedMethod,
  };
}

export function logRouteTouch(
  surface: RouteTouchSurface,
  path: string,
  method: string,
  metrics?: RouteTouchMetrics,
): RouteTouch {
  const touch = resolveRouteTouch(path, method);
  logger.debug("api.route.touch", {
    component: "RouteInventory",
    action: "route.touch",
    metadata: {
      surface,
      invFamily: touch.family,
      invOp: touch.operation,
      routeTemplate: touch.routeTemplate,
      method: touch.method,
      ...(metrics?.status !== undefined ? { status: metrics.status } : {}),
      ...(metrics?.durationMs !== undefined
        ? { durationMs: metrics.durationMs }
        : {}),
    },
  });
  return touch;
}

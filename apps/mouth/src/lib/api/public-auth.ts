import { AuthApi, PORTAL_SUPERUSER_PROBE_ENDPOINT } from "./auth/auth.api";
import type {
  ApiRequestOptions,
  CredentialedResponse,
} from "./types/api-client.types";
import type { UserProfile } from "@/types";
import { safeStorage } from "@/lib/utils/storage";
import { getTokenSubject } from "@/lib/utils/token";
import {
  AUTH_TOKEN_STORAGE_KEY,
  USER_PROFILE_STORAGE_KEY,
  notifySessionChanged,
} from "@/lib/auth/session-signal";

const PUBLIC_AUTH_ENDPOINTS = new Set([
  "/api/auth/login",
  "/api/auth/logout",
  "/api/auth/profile",
  // Post-login destination probe for staff on the portal gate. It answers a
  // boolean about the caller and nothing else — no client data can cross here.
  PORTAL_SUPERUSER_PROBE_ENDPOINT,
]);
const MAGIC_LINK_ENDPOINT = /^\/api\/auth\/verify-magic\/[^/?#]+$/;

function isPublicAuthEndpoint(endpoint: string): boolean {
  return (
    PUBLIC_AUTH_ENDPOINTS.has(endpoint) || MAGIC_LINK_ENDPOINT.test(endpoint)
  );
}

class PublicAuthRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "PublicAuthRequestError";
  }
}

/** Minimal transport used only by unauthenticated auth screens. */
export class PublicAuthClient {
  private token: string | null = null;
  private csrfToken: string | null = null;

  async request<T>(
    endpoint: string,
    options: ApiRequestOptions = {},
    timeoutMs = 30_000,
  ): Promise<T> {
    return this.perform<T>(endpoint, options, timeoutMs, { token: null });
  }

  /** request() plus the bearer actually sent (SESSION.md S3/S4); never log the token. */
  async requestWithCredential<T>(
    endpoint: string,
    options: ApiRequestOptions = {},
    timeoutMs = 30_000,
  ): Promise<CredentialedResponse<T>> {
    const sent: { token: string | null } = { token: null };
    const data = await this.perform<T>(endpoint, options, timeoutMs, sent);
    return {
      data,
      sentToken: sent.token,
      sentSub: getTokenSubject(sent.token),
    };
  }

  private async perform<T>(
    endpoint: string,
    options: ApiRequestOptions,
    timeoutMs: number,
    sent: { token: string | null },
  ): Promise<T> {
    if (!isPublicAuthEndpoint(endpoint)) {
      throw new Error("Public auth client rejected a non-auth endpoint");
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    const method = (options.method ?? "GET").toUpperCase();
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...((options.headers as Record<string, string>) ?? {}),
    };

    if (this.csrfToken && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      headers["X-CSRF-Token"] = this.csrfToken;
    }
    if (this.token) {
      headers.Authorization = `Bearer ${this.token}`;
      sent.token = this.token;
    }

    try {
      const response = await fetch(endpoint, {
        ...options,
        headers,
        credentials: "include",
        signal: controller.signal,
      });
      const body = await response.json().catch(() => ({}));

      if (!response.ok) {
        const detail =
          typeof body?.detail === "string"
            ? body.detail
            : `Authentication request failed (${response.status})`;
        throw new PublicAuthRequestError(detail, response.status);
      }

      return body as T;
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        throw new PublicAuthRequestError("Authentication request timed out", 0);
      }
      throw error;
    } finally {
      clearTimeout(timeoutId);
    }
  }

  setToken(token: string): void {
    this.token = token;
    safeStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
    notifySessionChanged();
  }

  setUserProfile(profile: UserProfile): void {
    safeStorage.setItem(USER_PROFILE_STORAGE_KEY, JSON.stringify(profile));
    notifySessionChanged();
  }

  setCsrfToken(token: string): void {
    this.csrfToken = token;
  }

  clearToken(): void {
    this.token = null;
    this.csrfToken = null;
    safeStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    safeStorage.removeItem(USER_PROFILE_STORAGE_KEY);
    notifySessionChanged();
  }
}

/**
 * Auth-only client for unauthenticated pages.
 *
 * Do not replace this with the `@/lib/api` singleton: that compatibility
 * barrel eagerly constructs every workspace domain client and would expose
 * internal API routes in the public login bundle.
 */
const publicAuthClient = new PublicAuthClient();

export const publicAuth = new AuthApi(publicAuthClient);

import type { IApiClient, ApiRequestOptions } from "../types/api-client.types";
import { UserProfile } from "@/types";
import type { BackendLoginResponse, LoginResponse } from "./auth.types";
import { logger } from "@/lib/logger";
import { getSessionEpoch } from "@/lib/auth/session-signal";
import {
  readStoredProfileFor,
  readStoredValidSub,
  readStoredValidToken,
} from "@/lib/auth/session-credential";

type AuthApiClient = Pick<
  IApiClient,
  | "request"
  | "requestWithCredential"
  | "setToken"
  | "setUserProfile"
  | "setCsrfToken"
  | "clearToken"
>;

/**
 * getProfile() answered a session this tab no longer holds (the credential it
 * sent is not the one in storage any more) and no profile of the current
 * session is stored to give back instead. Callers treat it like any failed
 * profile read. See src/lib/auth/SESSION.md S3.
 */
export class StaleSessionError extends Error {
  constructor() {
    super("The session changed while the profile was loading");
    this.name = "StaleSessionError";
  }
}

/**
 * Authentication API methods
 */
/**
 * Read-only probe answering whether the signed-in account is a portal
 * superuser (may impersonate a client via `?as_client=<id>`). It is a mouth
 * route handler that reaches the backend's `/api/portal/admin/me` server-side:
 * the public login bundle may only name `/api/auth/*` routes
 * (`scripts/assert-public-login-bundle.mjs`). Shared with the public auth
 * client's endpoint allowlist so the two cannot drift apart.
 */
export const PORTAL_SUPERUSER_PROBE_ENDPOINT = "/api/auth/portal-superuser";
// The probe runs on every staff sign-in BEFORE the redirect timer starts, so a
// hanging request must not hold the "access granted" screen: past this it is
// read as "no" and the sign-in proceeds to the backend destination.
export const PORTAL_SUPERUSER_PROBE_TIMEOUT_MS = 5_000;

export class AuthApi {
  constructor(private client: AuthApiClient) {}

  async login(email: string, pin: string): Promise<LoginResponse> {
    logger.debug("Login attempt started", {
      component: "AuthApi",
      action: "login",
    });

    try {
      const response = await this.client.request<BackendLoginResponse>(
        "/api/auth/login",
        {
          method: "POST",
          body: JSON.stringify({ email, pin }),
        },
        90000, // 90s timeout — backend may cold-start on Fly.io
      );

      logger.debug("API response received", {
        component: "AuthApi",
        action: "login_response",
      });

      if (!response.success || !response.data) {
        logger.error("Login failed - invalid response", {
          component: "AuthApi",
          action: "login_failed",
        });
        throw new Error(response.message || "Login failed");
      }

      // Save CSRF token from response (cookie is also set by backend)
      if (response.data.csrfToken) {
        logger.debug("Setting CSRF token", {
          component: "AuthApi",
          action: "set_csrf",
        });
        this.client.setCsrfToken(response.data.csrfToken);
      }

      // Save token to localStorage (optional enhancement - httpOnly cookies are primary auth)
      logger.debug("Saving token to localStorage (optional)", {
        component: "AuthApi",
        action: "save_token",
      });
      this.client.setToken(response.data.token);
      this.client.setUserProfile(response.data.user);

      logger.info("Login successful", {
        component: "AuthApi",
        action: "login_success",
      });

      // Return frontend-friendly format
      return {
        access_token: response.data.token,
        token_type: response.data.token_type,
        user: response.data.user,
        ...(response.data.redirectTo
          ? { redirectTo: response.data.redirectTo }
          : {}),
      };
    } catch (error) {
      const status = (error as { status?: unknown })?.status;
      if (typeof status === "number" && status >= 400 && status < 500) {
        logger.info("Login denied", {
          component: "AuthApi",
          action: "login_denied",
          code: status,
        });
      } else {
        logger.error("Login error", {
          component: "AuthApi",
          action: "login_error",
        });
      }
      throw error;
    }
  }

  /**
   * FASE 6 — consume a passwordless magic-link token and establish a session.
   *
   * Mirrors `login`: the backend sets the httpOnly cookie and returns the same
   * payload (token + csrfToken + user), which we persist the same way.
   */
  async verifyMagicLink(token: string): Promise<LoginResponse> {
    const response = await this.client.request<BackendLoginResponse>(
      `/api/auth/verify-magic/${encodeURIComponent(token)}`,
      { method: "GET" },
      90000,
    );

    if (!response.success || !response.data) {
      throw new Error(
        response.message || "This sign-in link is invalid or expired.",
      );
    }

    if (response.data.csrfToken) {
      this.client.setCsrfToken(response.data.csrfToken);
    }
    this.client.setToken(response.data.token);
    this.client.setUserProfile(response.data.user);

    return {
      access_token: response.data.token,
      token_type: response.data.token_type,
      user: response.data.user,
      ...(response.data.redirectTo
        ? { redirectTo: response.data.redirectTo }
        : {}),
    };
  }

  async logout(): Promise<void> {
    // Start the authenticated request before clearing local state so the
    // request captures the current Authorization/CSRF credentials. Do not wait
    // for the network before invalidating the in-memory and persisted session.
    const serverInvalidation = this.client.request<void>("/api/auth/logout", {
      method: "POST",
    });
    this.client.clearToken();
    await serverInvalidation;
  }

  /**
   * auth-gates-cookie-primary round 2: `/api/auth/profile` is bearer-only
   * (FastAPI 0.141.1's HTTPBearer answers 401 — not 403 — to a request with
   * no Authorization header, even one carrying a VALID cookie session).
   * Callers that plan to classify that failure themselves (the workspace
   * layout, useChatPage, the analytics founder gate) pass
   * `{ redirectOnUnauthorized: false }` so request()'s own 401 handler does
   * not act on their behalf before they get a chance to ask hasSession().
   */
  async getProfile(options?: ApiRequestOptions): Promise<UserProfile> {
    // A profile answers the credential that was SENT, not the one in storage
    // when the answer lands (SESSION.md S3). The epoch alone cannot prove
    // that: another tab's `storage` events are queued and may be handled after
    // this continuation.
    const issuedEpoch = getSessionEpoch();
    if (typeof this.client.requestWithCredential !== "function") {
      // Test doubles only: both real clients implement it.
      const profile = await this.client.request<UserProfile>(
        "/api/auth/profile",
        options ?? {},
      );
      if (getSessionEpoch() === issuedEpoch)
        this.client.setUserProfile(profile);
      return profile;
    }
    const { data: profile, sentToken } =
      await this.client.requestWithCredential<UserProfile>(
        "/api/auth/profile",
        options ?? {},
      );
    // Cookie-only request: nothing to compare against and nothing to write
    // (the endpoint is bearer-only, so this is in practice a 401 above).
    if (sentToken === null) return profile;

    // FRESH read of storage, not an event or the epoch. The stored token must
    // be CURRENTLY VALID (the same skew as the session identity) and be the
    // one that was sent: a token that expired while the request was in
    // flight no longer identifies anybody, so its profile is neither written
    // nor returned.
    if (readStoredValidToken() === sentToken) {
      // Same credential still in force. The epoch is the extra same-tab guard
      // for the WRITE only: a moved epoch returns the response unwritten.
      if (getSessionEpoch() === issuedEpoch)
        this.client.setUserProfile(profile);
      return profile;
    }

    // The credential that fetched this profile left the tab (or expired). Never write it.
    // Give back the stored profile only if it belongs to the session now in
    // force; otherwise there is nothing truthful to return.
    const currentSub = readStoredValidSub();
    const current = currentSub
      ? readStoredProfileFor<UserProfile>(currentSub)
      : null;
    if (current) return current;
    throw new StaleSessionError();
  }

  /**
   * Whether the signed-in account may use the client portal as a superuser.
   * The backend answers `is_superuser=false` for everyone else and never
   * throws on its own; a transport failure is read as "no" so a sign-in can
   * never break on this probe.
   */
  async isPortalSuperuser(): Promise<boolean> {
    try {
      const me = await this.client.request<{ is_superuser?: boolean }>(
        PORTAL_SUPERUSER_PROBE_ENDPOINT,
        {},
        PORTAL_SUPERUSER_PROBE_TIMEOUT_MS,
      );
      return me?.is_superuser === true;
    } catch {
      return false;
    }
  }
}

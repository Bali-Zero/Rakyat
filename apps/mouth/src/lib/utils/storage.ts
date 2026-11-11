/**
 * Safe localStorage wrapper with fallback for Safari Private Browsing
 *
 * Safari iOS blocks localStorage in Private Browsing mode, throwing QuotaExceededError.
 * This wrapper gracefully handles those cases without crashing the app.
 *
 * Best Practice: Use httpOnly cookies for auth (primary), localStorage as optional enhancement.
 */

import { logger } from "../logger";
import { toError } from "../types/common";

class SafeStorage {
  private isAvailable: boolean;
  private memoryFallback: Map<string, string>;
  /**
   * Keys whose CURRENT value lives in the memory fallback because a write to
   * localStorage was rejected (quota) after the initial probe passed. Reads of
   * these keys must prefer the fallback: localStorage, if it holds anything
   * for the key, holds an OLDER value (possibly another person's token).
   */
  private fallbackHeld: Set<string>;

  constructor() {
    this.memoryFallback = new Map();
    this.fallbackHeld = new Set();
    this.isAvailable = this.checkAvailability();
  }

  /**
   * Check if localStorage is available (not blocked by Private Browsing)
   */
  private checkAvailability(): boolean {
    if (typeof window === "undefined") return false;

    try {
      const testKey = "__storage_test__";
      localStorage.setItem(testKey, "test");
      localStorage.removeItem(testKey);
      return true;
    } catch (e) {
      logger.warn(
        "[SafeStorage] localStorage blocked (Private Browsing or disabled). Using memory fallback.",
        { component: "SafeStorage", action: "checkAvailability" },
      );
      return false;
    }
  }

  /**
   * Safely get item from localStorage with fallback
   */
  getItem(key: string): string | null {
    try {
      if (this.fallbackHeld.has(key)) {
        return this.memoryFallback.get(key) || null;
      }
      if (this.isAvailable) {
        return localStorage.getItem(key);
      }
      return this.memoryFallback.get(key) || null;
    } catch (e) {
      logger.warn(
        `[SafeStorage] getItem failed for key "${key}"`,
        { component: "SafeStorage", action: "getItem", metadata: { key } },
        toError(e),
      );
      return this.memoryFallback.get(key) || null;
    }
  }

  /**
   * Safely set item in localStorage with fallback
   */
  setItem(key: string, value: string): boolean {
    if (!this.isAvailable) {
      this.memoryFallback.set(key, value);
      return false; // Indicate fallback was used
    }
    try {
      localStorage.setItem(key, value);
      // localStorage now holds the current value: drop any fallback copy.
      this.memoryFallback.delete(key);
      this.fallbackHeld.delete(key);
      return true;
    } catch (e) {
      logger.warn(
        `[SafeStorage] setItem failed for key "${key}"`,
        { component: "SafeStorage", action: "setItem", metadata: { key } },
        toError(e),
      );
      // The current value now lives in memory. Mark the key so reads prefer it
      // even if the removal below fails, and try to take the OLDER value out
      // of shared storage so no other reader (or tab) mistakes it for current.
      this.memoryFallback.set(key, value);
      this.fallbackHeld.add(key);
      try {
        localStorage.removeItem(key);
      } catch {
        // reads still prefer the fallback for this key
      }
      return false;
    }
  }

  /**
   * Safely remove item from localStorage with fallback
   */
  removeItem(key: string): void {
    try {
      if (this.isAvailable) {
        localStorage.removeItem(key);
      }
    } catch (e) {
      logger.warn(
        `[SafeStorage] removeItem failed for key "${key}"`,
        { component: "SafeStorage", action: "removeItem", metadata: { key } },
        toError(e),
      );
    } finally {
      this.memoryFallback.delete(key);
      this.fallbackHeld.delete(key);
    }
  }

  /**
   * Clear all items (both localStorage and memory fallback)
   */
  clear(): void {
    try {
      if (this.isAvailable) {
        localStorage.clear();
      }
    } catch (e) {
      logger.warn(
        "[SafeStorage] clear failed",
        { component: "SafeStorage", action: "clear" },
        toError(e),
      );
    } finally {
      this.memoryFallback.clear();
      this.fallbackHeld.clear();
    }
  }

  /**
   * Another tab changed shared storage. For a key this tab holds in the
   * fallback, any change settles it: a non-null newValue is a newer shared
   * value (the shared value wins), null is a removal (removal wins, e.g. a
   * logout). Either way the fallback copy and its mark are dropped. A null
   * key is storage.clear(). Ordering: see the listener installation below.
   * @internal
   */
  _onStorageEvent(key: string | null): void {
    if (key === null) {
      this.memoryFallback.clear();
      this.fallbackHeld.clear();
      return;
    }
    if (this.fallbackHeld.has(key)) {
      this.memoryFallback.delete(key);
      this.fallbackHeld.delete(key);
    }
  }

  /**
   * Check if localStorage is actually available (not using fallback)
   */
  isLocalStorageAvailable(): boolean {
    return this.isAvailable;
  }

  /**
   * Reset internal state for testing purposes.
   * Re-checks localStorage availability and clears memory fallback.
   * @internal Only use in tests
   */
  _resetForTesting(): void {
    this.memoryFallback.clear();
    this.fallbackHeld.clear();
    this.isAvailable = this.checkAvailability();
  }
}

// Singleton instance
export const safeStorage = new SafeStorage();

/**
 * Convergence across tabs (SESSION.md S8). Listener ordering: this listener
 * must run BEFORE any listener that reads session keys through safeStorage.
 * DOM listeners on one target run in registration order, and this one is
 * registered when this module is evaluated. Every module that registers a
 * session-key `storage` listener at load time imports `session-signal.ts`,
 * which imports this module, so ES module dependency order evaluates this
 * file first; listeners added later at runtime (subscribeSessionChange, the
 * hook) come after by construction. Browser only, installed once.
 */
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    safeStorage._onStorageEvent(event.key);
  });
}

/**
 * Backward compatible exports for drop-in replacement
 */
export const storage = {
  getItem: (key: string) => safeStorage.getItem(key),
  setItem: (key: string, value: string) => safeStorage.setItem(key, value),
  removeItem: (key: string) => safeStorage.removeItem(key),
  clear: () => safeStorage.clear(),
  isAvailable: () => safeStorage.isLocalStorageAvailable(),
};

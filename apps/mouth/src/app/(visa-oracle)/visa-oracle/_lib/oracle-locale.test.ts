import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  VISIT_LOCALE_STORAGE_KEY,
  localeFromLocationSearch,
  oracleInternalHref,
  parseOracleLocale,
  readVisitLocaleHint,
  replaceOracleHistoryUrl,
  resolveVisitLocale,
  syncVisitLocaleInAddressBar,
  visitLocaleUrlFromParts,
  writeVisitLocaleHint,
} from "./oracle-locale";

function createMemorySessionStorage() {
  const store: Record<string, string> = {};
  return {
    store,
    getItem(key: string) {
      return store[key] ?? null;
    },
    setItem(key: string, value: string) {
      store[key] = value;
    },
    removeItem(key: string) {
      delete store[key];
    },
    clear() {
      for (const key of Object.keys(store)) delete store[key];
    },
  };
}

describe("oracle-locale", () => {
  let memoryStorage: ReturnType<typeof createMemorySessionStorage>;

  beforeEach(() => {
    memoryStorage = createMemorySessionStorage();
    vi.stubGlobal("sessionStorage", memoryStorage);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to English when lang is absent or unknown", () => {
    expect(parseOracleLocale(undefined)).toBe("en");
    expect(parseOracleLocale(null)).toBe("en");
    expect(parseOracleLocale("en")).toBe("en");
    expect(parseOracleLocale("it")).toBe("en");
  });

  it("accepts only id as Indonesian", () => {
    expect(parseOracleLocale("id")).toBe("id");
  });

  it("appends ?lang=id on the two oracle paths when language is id", () => {
    expect(oracleInternalHref("/visa-oracle/privacy", "en")).toBe(
      "/visa-oracle/privacy",
    );
    expect(oracleInternalHref("/visa-oracle/privacy", "id")).toBe(
      "/visa-oracle/privacy?lang=id",
    );
    expect(oracleInternalHref("/visa-oracle", "id")).toBe(
      "/visa-oracle?lang=id",
    );
    expect(oracleInternalHref("/visa-oracle", "en")).toBe("/visa-oracle");
  });

  it("visitLocaleUrlFromParts changes only lang and keeps other query keys and hash", () => {
    expect(
      visitLocaleUrlFromParts("/visa-oracle", "?foo=bar", "#section", "id"),
    ).toBe("/visa-oracle?foo=bar&lang=id#section");
    expect(
      visitLocaleUrlFromParts(
        "/visa-oracle/privacy",
        "?foo=bar&lang=id",
        "",
        "en",
      ),
    ).toBe("/visa-oracle/privacy?foo=bar");
  });

  it("localeFromLocationSearch reads lang param", () => {
    expect(localeFromLocationSearch("?lang=id")).toBe("id");
    expect(localeFromLocationSearch("?foo=1")).toBe("en");
  });

  describe("resolveVisitLocale", () => {
    it("fresh navigate honors URL over stale hint", () => {
      expect(
        resolveVisitLocale({
          urlLanguage: "en",
          visitHint: "id",
          navigationType: "navigate",
        }),
      ).toBe("en");
      expect(
        resolveVisitLocale({
          urlLanguage: "id",
          visitHint: "en",
          navigationType: "navigate",
        }),
      ).toBe("id");
    });

    it("back_forward and reload favor hint over stale URL", () => {
      expect(
        resolveVisitLocale({
          urlLanguage: "en",
          visitHint: "id",
          navigationType: "back_forward",
        }),
      ).toBe("id");
      expect(
        resolveVisitLocale({
          urlLanguage: "en",
          visitHint: "id",
          navigationType: "reload",
        }),
      ).toBe("id");
    });

    it("unknown navigation with stale hint still honors URL", () => {
      expect(
        resolveVisitLocale({
          urlLanguage: "en",
          visitHint: "id",
          navigationType: "unknown",
        }),
      ).toBe("en");
    });

    it("popstate and pageshow.persisted favor hint", () => {
      expect(
        resolveVisitLocale({
          urlLanguage: "en",
          visitHint: "id",
          navigationType: "unknown",
          popstate: true,
        }),
      ).toBe("id");
      expect(
        resolveVisitLocale({
          urlLanguage: "en",
          visitHint: "en",
          navigationType: "navigate",
          pageshowPersisted: true,
        }),
      ).toBe("en");
    });
  });

  it("visit hint read/write tolerates storage errors", () => {
    writeVisitLocaleHint("id");
    expect(readVisitLocaleHint()).toBe("id");
    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    });
    expect(readVisitLocaleHint()).toBeNull();
    expect(() => writeVisitLocaleHint("en")).not.toThrow();
  });

  it("replaceOracleHistoryUrl uses null then restores custom state without new entries", () => {
    const replaceState = vi.fn();
    const prior = { qaMarker: "stay", nested: { n: 1 } };
    const location = {
      pathname: "/visa-oracle",
      search: "?utm_source=qa",
      hash: "#proof",
    };
    let length = 3;
    vi.stubGlobal("window", {
      location,
      history: {
        get length() {
          return length;
        },
        state: prior,
        replaceState,
      },
    });
    replaceOracleHistoryUrl("/visa-oracle?utm_source=qa&lang=id#proof");
    expect(length).toBe(3);
    expect(replaceState).toHaveBeenCalledTimes(2);
    expect(replaceState.mock.calls[0]).toEqual([
      null,
      "",
      "/visa-oracle?utm_source=qa&lang=id#proof",
    ]);
    expect(replaceState.mock.calls[1]).toEqual([prior, ""]);
    expect(length).toBe(3);
    vi.unstubAllGlobals();
  });

  it("syncVisitLocaleInAddressBar skips when URL is already correct", () => {
    const replaceState = vi.fn();
    const location = {
      pathname: "/visa-oracle",
      search: "?lang=id",
      hash: "",
    };
    vi.stubGlobal("window", {
      location,
      history: { replaceState, state: null, length: 2 },
      sessionStorage: globalThis.sessionStorage,
    });
    syncVisitLocaleInAddressBar("id");
    expect(replaceState).not.toHaveBeenCalled();
    expect(memoryStorage.store[VISIT_LOCALE_STORAGE_KEY]).toBe("id");
    vi.unstubAllGlobals();
  });

  it("syncVisitLocaleInAddressBar replaces URL and stores hint", () => {
    const replaceState = vi.fn();
    const location = {
      pathname: "/visa-oracle",
      search: "",
      hash: "",
    };
    vi.stubGlobal("window", {
      location,
      history: { replaceState, state: { marker: 1 }, length: 2 },
      sessionStorage: globalThis.sessionStorage,
    });
    syncVisitLocaleInAddressBar("id");
    expect(replaceState).toHaveBeenCalledTimes(2);
    expect(replaceState.mock.calls[0][2]).toBe("/visa-oracle?lang=id");
    vi.unstubAllGlobals();
  });
});

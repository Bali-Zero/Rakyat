import { describe, expect, it, vi } from "vitest";
import {
  oracleInternalHref,
  parseOracleLocale,
  syncVisitLocaleInAddressBar,
  visitLocaleUrlFromParts,
} from "./oracle-locale";

describe("oracle-locale", () => {
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

  it("syncVisitLocaleInAddressBar replaceState skips when URL is already correct", () => {
    const replaceState = vi.fn();
    const location = {
      pathname: "/visa-oracle",
      search: "?lang=id",
      hash: "",
    };
    vi.stubGlobal("window", {
      location,
      history: { replaceState, state: { idx: 1 } },
    });
    syncVisitLocaleInAddressBar("id");
    expect(replaceState).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("syncVisitLocaleInAddressBar replaceState sets or removes lang", () => {
    const replaceState = vi.fn();
    const location = {
      pathname: "/visa-oracle",
      search: "",
      hash: "",
    };
    vi.stubGlobal("window", {
      location,
      history: { replaceState, state: { idx: 1 } },
    });
    syncVisitLocaleInAddressBar("id");
    expect(replaceState).toHaveBeenCalledWith(
      { idx: 1 },
      "",
      "/visa-oracle?lang=id",
    );
    location.search = "?lang=id&foo=bar";
    syncVisitLocaleInAddressBar("en");
    expect(replaceState).toHaveBeenLastCalledWith(
      { idx: 1 },
      "",
      "/visa-oracle?foo=bar",
    );
    vi.unstubAllGlobals();
  });
});

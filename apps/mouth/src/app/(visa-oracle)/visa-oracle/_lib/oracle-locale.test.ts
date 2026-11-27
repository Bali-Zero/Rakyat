import { describe, expect, it } from "vitest";
import { oracleInternalHref, parseOracleLocale } from "./oracle-locale";

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
});

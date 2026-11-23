import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  EMPTY_PPH,
  PPH_MAX_RATE_PERCENT,
  PPH_SETTER_ROLES,
  PPH_TYPE_LABELS,
  canSetPph,
  pphChanged,
  pphError,
  pphFromMetadata,
  pphSelection,
} from "./PphWithholdingField";

const BACKEND = join(__dirname, "../../../../../backend-rag/backend");
const quoted = (block: string) =>
  [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]);

describe("PPh field mirrors the backend contract", () => {
  const pph = readFileSync(join(BACKEND, "services/invoicing/pph.py"), "utf8");
  const crm = readFileSync(join(BACKEND, "app/utils/crm_utils.py"), "utf8");

  it("same types and labels as PPH_TYPE_LABELS", () => {
    const block = pph.match(/PPH_TYPE_LABELS[^{]*\{([\s\S]*?)\}/)?.[1] ?? "";
    const pairs = quoted(block);
    expect(pairs.length).toBeGreaterThan(0);
    const backend: Record<string, string> = {};
    for (let i = 0; i < pairs.length; i += 2) backend[pairs[i]] = pairs[i + 1];
    expect(backend).toEqual(PPH_TYPE_LABELS);
  });

  it("same maximum rate", () => {
    expect(pph).toContain(
      `PPH_MAX_RATE_PERCENT = Decimal("${PPH_MAX_RATE_PERCENT}")`,
    );
  });

  it("same roles as COMPANY_CREATE_ROLES", () => {
    const block =
      crm.match(/COMPANY_CREATE_ROLES = frozenset\(([\s\S]*?)\n\)/)?.[1] ?? "";
    const roles = quoted(block);
    expect(roles.length).toBeGreaterThan(0);
    expect([...roles].sort()).toEqual([...PPH_SETTER_ROLES].sort());
  });
});

describe("PPh draft", () => {
  it("empty is valid and sends nothing", () => {
    expect(pphError(EMPTY_PPH)).toBeNull();
    expect(pphSelection(EMPTY_PPH)).toBeNull();
  });

  it("a filled draft becomes the API value, with a comma or a dot", () => {
    expect(pphSelection({ type: "PPH23", rate: "2" })).toEqual({
      type: "PPH23",
      rate_percent: 2,
    });
    expect(pphSelection({ type: "PPH4_2", rate: "2,5" })).toEqual({
      type: "PPH4_2",
      rate_percent: 2.5,
    });
  });

  it.each(["0", "10.01", "abc", "1.234", "-2"])("rejects rate %s", (rate) => {
    expect(pphError({ type: "PPH23", rate })).not.toBeNull();
    expect(pphSelection({ type: "PPH23", rate })).toBeNull();
  });

  it("a half-filled draft is an error", () => {
    expect(pphError({ type: "PPH23", rate: "" })).not.toBeNull();
    expect(pphError({ type: "", rate: "2" })).not.toBeNull();
  });

  it("reads metadata as an object or as JSON text", () => {
    const meta = { pph: { type: "PPH23", rate_percent: 2 } };
    const expected = { type: "PPH23", rate: "2" };
    expect(pphFromMetadata(meta)).toEqual(expected);
    expect(pphFromMetadata(JSON.stringify(meta))).toEqual(expected);
    expect(pphFromMetadata(JSON.stringify(JSON.stringify(meta)))).toEqual(
      expected,
    );
  });

  it.each([
    null,
    undefined,
    "not json",
    {},
    { pph: { type: "X" } },
    { pph: { type: "PPH23", rate_percent: 2.125 } },
    { pph: { type: "PPH23", rate_percent: 11 } },
  ])(
    "treats unreadable metadata as empty, as the invoice does: %j",
    (metadata) => {
      expect(pphFromMetadata(metadata)).toEqual(EMPTY_PPH);
    },
  );

  it("compares by value, so 2 and 2.00 are not a change", () => {
    const saved = { type: "PPH23", rate: "2" } as const;
    expect(pphChanged(saved, { type: "PPH23", rate: "2.00" })).toBe(false);
    expect(pphChanged(saved, { type: "PPH23", rate: "3" })).toBe(true);
    expect(pphChanged(saved, EMPTY_PPH)).toBe(true);
  });

  it("only the tax team and admins may set it", () => {
    expect(canSetPph(true, "consultant")).toBe(true);
    expect(canSetPph(false, " Tax Lead ")).toBe(true);
    expect(canSetPph(false, "consultant")).toBe(false);
    expect(canSetPph(false, undefined)).toBe(false);
  });
});

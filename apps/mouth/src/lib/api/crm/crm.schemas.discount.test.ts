import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createPracticeSchema } from "./crm.schemas";
import {
  DISCOUNT_COVERS_PRICE_DETAIL,
  DISCOUNT_COVERS_PRICE_TEXT_ID,
  QUOTED_PRICE_REQUIRED_DETAIL,
  QUOTED_PRICE_REQUIRED_TEXT_ID,
  invoiceAmountRefusalText,
} from "./state-machine";

const NEEDS_PRICE = "Isi Quoted Price dulu sebelum mengisi Discount";
const base = { client_id: 42, practice_type_code: undefined };

function discountErrors(values: {
  quoted_price?: number;
  discount_amount?: number;
}): string[] {
  const result = createPracticeSchema.safeParse({ ...base, ...values });
  if (result.success) return [];
  return result.error.issues
    .filter((issue) => issue.path[0] === "discount_amount")
    .map((issue) => issue.message);
}

describe("createPracticeSchema discount (DAI-177)", () => {
  it("refuses a discount without a Quoted Price", () => {
    expect(discountErrors({ discount_amount: 100000 })).toEqual([NEEDS_PRICE]);
  });

  it("accepts a discount below the Quoted Price", () => {
    expect(
      discountErrors({ quoted_price: 1470000, discount_amount: 100000 }),
    ).toEqual([]);
  });

  it("still refuses a discount above the Quoted Price", () => {
    expect(
      discountErrors({ quoted_price: 1000000, discount_amount: 1470000 }),
    ).toEqual(["Discount harus lebih kecil dari Quoted Price"]);
  });

  it("refuses a discount equal to the Quoted Price", () => {
    expect(
      discountErrors({ quoted_price: 1470000, discount_amount: 1470000 }),
    ).toEqual(["Discount harus lebih kecil dari Quoted Price"]);
  });

  it("accepts a zero discount on a zero price", () => {
    expect(discountErrors({ quoted_price: 0, discount_amount: 0 })).toEqual([]);
  });

  it("accepts no discount and a zero discount without a price", () => {
    expect(discountErrors({})).toEqual([]);
    expect(discountErrors({ discount_amount: 0 })).toEqual([]);
  });

  it("names the reason when the backend refuses the move", () => {
    expect(
      invoiceAmountRefusalText({ detail: DISCOUNT_COVERS_PRICE_DETAIL }),
    ).toBe(DISCOUNT_COVERS_PRICE_TEXT_ID);
    expect(
      invoiceAmountRefusalText(new Error(QUOTED_PRICE_REQUIRED_DETAIL)),
    ).toBe(QUOTED_PRICE_REQUIRED_TEXT_ID);
    expect(invoiceAmountRefusalText(new Error("other"))).toBeNull();
  });

  it("the new-process form validates both amounts", () => {
    const page = readFileSync(
      join(__dirname, "../../../app/(workspace)/process/new/page.tsx"),
      "utf8",
    );
    const call = page.slice(
      page.indexOf("createPracticeSchema.safeParse("),
      page.indexOf("if (!result.success)"),
    );
    expect(call).toContain("quoted_price:");
    expect(call).toContain("discount_amount:");
  });
});

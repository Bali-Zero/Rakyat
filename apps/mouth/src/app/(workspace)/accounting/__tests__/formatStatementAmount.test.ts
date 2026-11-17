import { describe, it, expect } from "vitest";
import { formatStatementAmount as f } from "../_components/formatStatementAmount";

describe("formatStatementAmount", () => {
  it.each([
    ["1500000.00", "Rp 1.500.000"],
    ["1500000.55", "Rp 1.500.000,55"],
    ["-250000.50", "-Rp 250.000,50"],
    ["0.00", "Rp 0"],
    ["361900825.55", "Rp 361.900.825,55"],
    ["9999999999999999.99", "Rp 9.999.999.999.999.999,99"],
    ["not money", "not money"],
    ["12", "12"],
    ["", ""],
    ["1e5.00", "1e5.00"],
  ])("%s -> %s", (raw, out) => {
    expect(f(raw)).toBe(out);
  });
});

import { describe, expect, it } from "vitest";
import { serializeJsonLd } from "./serialize-json-ld";

const LS = String.fromCharCode(0x2028);
const PS = String.fromCharCode(0x2029);

const HOSTILE = [
  "</script><script>alert(1)</script>",
  "<!-- <script>",
  "a & b &amp; c > d < e",
  `line${LS}sep${PS}para`,
  '"quote" \\ back\\slash \n newline',
  "plain",
];

describe("serializeJsonLd", () => {
  it.each(HOSTILE)("round-trips %j", (text) => {
    const value = { a: text, nested: [{ b: text }], n: 1, t: true, z: null };
    expect(JSON.parse(serializeJsonLd(value))).toEqual(value);
  });

  it.each(HOSTILE)("leaves no HTML-significant character in %j", (text) => {
    const out = serializeJsonLd({ a: text });
    for (const ch of ["<", ">", "&", LS, PS]) {
      expect(out.includes(ch)).toBe(false);
    }
  });

  it("neutralises </script> and <!--", () => {
    const out = serializeJsonLd({ a: "</script><!--" });
    expect(out.toLowerCase()).not.toContain("</script");
    expect(out).not.toContain("<!--");
  });

  it("keys are escaped too", () => {
    const value = { "</script>": 1 };
    const out = serializeJsonLd(value);
    expect(out).not.toContain("<");
    expect(JSON.parse(out)).toEqual(value);
  });

  it("leaves ordinary JSON byte for byte", () => {
    const value = {
      "@context": "https://schema.org",
      name: "Bali Zero",
      n: [1, 2],
    };
    expect(serializeJsonLd(value)).toBe(JSON.stringify(value));
  });
});

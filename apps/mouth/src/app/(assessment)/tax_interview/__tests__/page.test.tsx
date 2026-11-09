import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const jar = vi.hoisted(() => ({ value: undefined as string | undefined }));
vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (n: string) =>
      n === "bz_tax_iv" && jar.value ? { value: jar.value } : undefined,
  }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

import { Interview } from "../_components/Interview";
import { PinGate } from "../_components/PinGate";
import { mintToken } from "../_server/access";
import Page from "../page";

beforeEach(() => vi.stubEnv("INTERNAL_API_KEY", "test-secret-not-real"));
afterEach(() => {
  vi.unstubAllEnvs();
  jar.value = undefined;
});

describe("/tax_interview page", () => {
  it("without a cookie renders only the PIN form: no content anywhere in the element tree", async () => {
    const el = (await Page()) as ReactElement;
    expect(el.type).toBe(PinGate);
    const dump = JSON.stringify(el);
    for (const s of ["Bu Lestari", "INV-108", "Satu Hari", "Misi", "Rp "])
      expect(dump).not.toContain(s);
  });

  it("with a tampered or expired cookie also renders only the PIN form", async () => {
    for (const v of [
      "1.abc",
      `${mintToken(Date.now() + 5000)}0`,
      mintToken(Date.now() - 5)!,
    ]) {
      jar.value = v;
      expect(((await Page()) as ReactElement).type).toBe(PinGate);
    }
  });

  it("with a valid cookie hands the story to the interview as props", async () => {
    jar.value = mintToken(Date.now() + 60_000)!;
    const el = (await Page()) as ReactElement<{
      content: { title: string; missions: unknown[] };
    }>;
    expect(el.type).toBe(Interview);
    expect(el.props.content.title).toBe("Satu Hari di Meja Pajak");
    expect(el.props.content.missions).toHaveLength(8);
  });

  it("never serialises a key into the props it hands over", async () => {
    jar.value = mintToken(Date.now() + 60_000)!;
    const dump = JSON.stringify(((await Page()) as ReactElement).props);
    for (const s of [
      "21.800.000",
      "BENAR",
      "SALAH",
      "terbaik",
      "rubrik",
      "Rubrik",
      "kunci",
    ])
      expect(dump, s).not.toContain(s);
  });
});

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const logged = vi.hoisted(() => vi.fn());
vi.mock("@/lib/logger", () => ({
  logger: { error: logged, info: logged, warn: logged, debug: logged },
}));

import { mintToken } from "../_server/access";
import { KEY } from "../_server/keys";
import { POST } from "@/app/api/tax-interview/submit/route";

const SECRET = "test-secret-not-real"; // pragma: allowlist secret
const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const NAME = "Calon Rahasia Uji";
const perfect = () => ({
  m1: [...KEY.m1.order],
  m2: { ...KEY.m2.pairs },
  m3: [...KEY.m3.wrong],
  m4: {
    q41ppn: "1650000",
    q41total: "16650000",
    q41mcq: "p1",
    q42: "21800000",
    q43w: "9600000",
    q43t: "86400000",
    q44: "6750000",
  },
  m5: { ...KEY.m5.rows, q13: "INV-108", q14: "t1" },
  m6: { checked: [...KEY.m6.right], note: "catatan rahasia" },
  m7: { reply: "balasan rahasia" },
  m8: Object.fromEntries(
    Object.entries(KEY.m8.best).map(([s, c]) => [
      s,
      { choice: c, reason: "alasan rahasia" },
    ]),
  ),
});
const post = (
  body: unknown,
  token: string | null | undefined = mintToken(Date.now() + 60_000),
) =>
  POST(
    new NextRequest("http://localhost/api/tax-interview/submit", {
      method: "POST",
      headers: token ? { cookie: `bz_tax_iv=${token}` } : {},
      body: typeof body === "string" ? body : JSON.stringify(body),
    }),
  );
const payload = (over: Record<string, unknown> = {}) => ({
  attemptId: ID,
  name: NAME,
  startedAt: Date.now() - 2_400_000,
  endedAt: Date.now(),
  answers: perfect(),
  telemetry: {},
  ...over,
});

const relay = vi.fn();
beforeEach(() => {
  vi.stubEnv("INTERNAL_API_KEY", SECRET);
  vi.stubEnv("NUZANTARA_API_URL", "http://relay.test/api");
  relay.mockReset().mockResolvedValue(
    new Response(JSON.stringify({ success: true, message: "ok" }), {
      status: 200,
    }),
  );
  vi.stubGlobal("fetch", relay);
  logged.mockClear();
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("POST /api/tax-interview/submit", () => {
  it("refuses without a cookie, with a tampered cookie and with an expired one; sends nothing", async () => {
    const t = mintToken(Date.now() + 60_000)!;
    for (const token of [
      null,
      `${t}x`,
      "9.abc",
      mintToken(Date.now() - 1000),
    ]) {
      const res = await post(payload(), token);
      expect(res.status).toBe(401);
    }
    expect(relay).not.toHaveBeenCalled();
  });

  it("sends exactly one email to the panel and tells the candidate nothing but ok", async () => {
    const res = await post(payload());
    expect(res.status).toBe(200);
    const text = await res.text();
    expect(JSON.parse(text)).toEqual({ ok: true });
    expect(text).not.toMatch(/\d/);
    expect(relay).toHaveBeenCalledTimes(1);
    const [url, init] = relay.mock.calls[0];
    expect(url).toBe("http://relay.test/api/notifications/send-email");
    expect(init.headers["X-API-Key"]).toBe(SECRET);
    const sent = JSON.parse(init.body);
    expect(sent.to).toBe("zero@balizero.com");
    expect(sent.cc).toBe("ari.firda@balizero.com,angel.tax@balizero.com");
    expect(sent.idempotency_key).toBe(ID);
    expect(sent.subject).toContain(NAME);
    expect(sent.subject).toContain("skor otomatis 76/76");
    expect(sent.body).toContain("Skor otomatis: 76 dari 76");
    expect(sent.body).toContain("balasan rahasia");
    expect(init.body).not.toContain(SECRET);
    expect(sent).not.toHaveProperty("from");
  });

  it("answers 502 and keeps the failure honest when the relay fails, throws or reports success:false", async () => {
    for (const r of [
      new Response("boom", { status: 500 }),
      new Response(JSON.stringify({ success: false }), { status: 200 }),
    ]) {
      relay.mockResolvedValueOnce(r);
      const res = await post(payload());
      expect(res.status).toBe(502);
      expect(await res.json()).toEqual({ ok: false, error: "SEND_FAILED" });
    }
    relay.mockRejectedValueOnce(new Error("network down with PIN 666"));
    expect((await post(payload())).status).toBe(502);
  });

  it("never logs the name, the answers or the PIN", async () => {
    relay.mockResolvedValueOnce(new Response("boom", { status: 500 }));
    await post(payload());
    relay.mockRejectedValueOnce(new Error(`down ${NAME} 666`));
    await post(payload());
    const dump = JSON.stringify(logged.mock.calls);
    expect(logged).toHaveBeenCalled();
    for (const secret of [NAME, "rahasia", "666", SECRET])
      expect(dump).not.toContain(secret);
  });

  it("validates the body: bad id, no name, junk, oversize, missing server secret", async () => {
    expect((await post(payload({ attemptId: "not-a-uuid" }))).status).toBe(400);
    expect((await post(payload({ name: "  " }))).status).toBe(400);
    expect((await post("{nope")).status).toBe(400);
    expect((await post("[]")).status).toBe(400);
    expect((await post(payload({ pad: "x".repeat(130_000) }))).status).toBe(
      413,
    );
    vi.stubEnv("INTERNAL_API_KEY", "");
    vi.stubEnv("API_KEY", "");
    expect((await post(payload(), "anything")).status).toBe(401);
    expect(relay).not.toHaveBeenCalled();
  });

  it("scores blank answers as 0 rather than failing", async () => {
    await post(payload({ answers: {}, telemetry: "junk" }));
    expect(JSON.parse(relay.mock.calls[0][1].body).subject).toContain(
      "skor otomatis 0/76",
    );
  });
});

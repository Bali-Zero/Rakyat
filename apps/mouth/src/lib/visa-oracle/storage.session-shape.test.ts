import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getRemainingQuestions,
  getSession,
  incrementQuestions,
  MAX_QUESTIONS,
  STORAGE_KEY,
} from "./storage";

const NOW = new Date("2026-05-23T04:00:00.000Z");

function store(raw: string): void {
  window.localStorage.setItem(STORAGE_KEY, raw);
}

describe("visa oracle storage: stored session shape", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it.each([
    ["an empty object", "{}"],
    ["an object without counters", '{"sessionId":"x"}'],
    ["an object without a session id", '{"questionsUsed":1,"createdAt":1}'],
    ["a blank session id", '{"sessionId":"","questionsUsed":0,"createdAt":1}'],
    [
      "a null counter",
      '{"sessionId":"x","questionsUsed":null,"createdAt":' +
        NOW.getTime() +
        "}",
    ],
    [
      "a negative counter",
      '{"sessionId":"x","questionsUsed":-1,"createdAt":' + NOW.getTime() + "}",
    ],
    ["a string", '"hello"'],
    ["null", "null"],
  ])("guilt: %s is treated as nothing stored", (_label, raw) => {
    store(raw);

    const session = getSession();

    expect(session.sessionId).toMatch(/^vo_/);
    expect(session.questionsUsed).toBe(0);
    expect(session.createdAt).toBe(NOW.getTime());

    store(raw);
    expect(getRemainingQuestions()).toBe(MAX_QUESTIONS);
    expect(Number.isFinite(getRemainingQuestions())).toBe(true);
  });

  it("guilt: a partial object never leaks null or a missing id into the next write", () => {
    store('{"sessionId":"x"}');

    const updated = incrementQuestions();

    expect(updated.questionsUsed).toBe(1);
    expect(typeof updated.sessionId).toBe("string");
    expect(updated.sessionId.length).toBeGreaterThan(0);
  });

  it("innocence: a well-formed stored session is returned unchanged", () => {
    const stored = {
      sessionId: "vo_known",
      questionsUsed: 2,
      createdAt: NOW.getTime() - 1000,
    };
    store(JSON.stringify(stored));

    expect(getSession()).toEqual(stored);
    expect(getRemainingQuestions()).toBe(MAX_QUESTIONS - 2);
  });

  it("innocence: an unparsable string still yields a fresh session", () => {
    store("{not json");

    const session = getSession();

    expect(session.questionsUsed).toBe(0);
    expect(session.sessionId).toMatch(/^vo_/);
  });
});

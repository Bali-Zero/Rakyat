import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { useWriteLock } from "../_components/useWriteLock";

type Op = "open" | "close" | "write" | "dialogWrite" | "done";
type Start = "idle" | "dialog" | "tabWrite" | "dialogWrite";
const OPS: Op[] = ["open", "close", "write", "dialogWrite", "done"];
const STARTS: Start[] = ["idle", "dialog", "tabWrite", "dialogWrite"];

const held = () => {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
};

/** The spec, as a table: what each operation is allowed to do from a state. */
type S = { pending: boolean; dialog: boolean };
const model = (s: S, op: Op): { allowed: boolean; next: S } => {
  switch (op) {
    case "open":
      return !s.pending && !s.dialog
        ? { allowed: true, next: { ...s, dialog: true } }
        : { allowed: false, next: s };
    case "close":
      return s.dialog && !s.pending
        ? { allowed: true, next: { ...s, dialog: false } }
        : { allowed: false, next: s };
    case "write":
      return !s.pending && !s.dialog
        ? { allowed: true, next: { ...s, pending: true } }
        : { allowed: false, next: s };
    case "dialogWrite":
      return s.dialog && !s.pending
        ? { allowed: true, next: { ...s, pending: true } }
        : { allowed: false, next: s };
    case "done":
      return { allowed: true, next: { ...s, dialog: false } };
  }
};

const startState = (
  h: ReturnType<typeof renderHook<ReturnType<typeof useWriteLock>, unknown>>,
  start: Start,
  gate: { resolve: () => void; promise: Promise<void> },
): S => {
  const l = () => h.result.current;
  act(() => {
    if (start === "dialog") l().tryOpenDialog();
    if (start === "tabWrite") void l().runWrite(() => gate.promise);
    if (start === "dialogWrite") {
      l().tryOpenDialog();
      void l().runDialogWrite(() => gate.promise);
    }
  });
  return {
    pending: start === "tabWrite" || start === "dialogWrite",
    dialog: start === "dialog" || start === "dialogWrite",
  };
};

describe("useWriteLock: every ordered pair, no render between", () => {
  for (const start of STARTS) {
    for (const first of OPS) {
      for (const second of OPS) {
        it(`${start}: ${first} then ${second}`, async () => {
          const h = renderHook(() => useWriteLock());
          const gate = held();
          let s = startState(h, start, gate);
          const fns = {
            write: vi.fn(() => held().promise),
            dialogWrite: vi.fn(() => held().promise),
          };
          const results: Record<string, unknown> = {};

          // Both calls happen against the SAME rendered `result.current`: no re-render between.
          const lock = h.result.current;
          const call = (op: Op, tag: string) => {
            if (op === "open") results[tag] = lock.tryOpenDialog();
            if (op === "close") results[tag] = lock.tryCloseDialog();
            if (op === "write") results[tag] = lock.runWrite(fns.write);
            if (op === "dialogWrite")
              results[tag] = lock.runDialogWrite(fns.dialogWrite);
            if (op === "done") lock.dialogDone();
          };
          const m1 = model(s, first);
          s = m1.next;
          const m2 = model(s, second);
          s = m2.next;
          act(() => {
            call(first, "a");
            call(second, "b");
          });

          const calledWrite = [
            first === "write" && m1.allowed,
            second === "write" && m2.allowed,
          ].filter(Boolean).length;
          const calledDialogWrite = [
            first === "dialogWrite" && m1.allowed,
            second === "dialogWrite" && m2.allowed,
          ].filter(Boolean).length;
          expect(fns.write).toHaveBeenCalledTimes(calledWrite);
          expect(fns.dialogWrite).toHaveBeenCalledTimes(calledDialogWrite);
          if (first === "open") expect(results.a).toBe(m1.allowed);
          if (second === "open") expect(results.b).toBe(m2.allowed);
          if (first === "close") expect(results.a).toBe(m1.allowed);
          if (second === "close") expect(results.b).toBe(m2.allowed);
          expect(h.result.current.pending).toBe(s.pending);
          expect(h.result.current.locked).toBe(s.pending || s.dialog);
          if (!m1.allowed && first === "write")
            await expect(results.a).resolves.toBe(false);
          if (!m2.allowed && second === "write")
            await expect(results.b).resolves.toBe(false);
          if (!m1.allowed && first === "dialogWrite")
            await expect(results.a).resolves.toBe(false);
          if (!m2.allowed && second === "dialogWrite")
            await expect(results.b).resolves.toBe(false);
          gate.resolve();
        });
      }
    }
  }
});

describe("useWriteLock: pending is always cleared", () => {
  it("a rejecting fn clears pending", async () => {
    const h = renderHook(() => useWriteLock());
    let p!: Promise<boolean>;
    act(() => {
      p = h.result.current.runWrite(() => Promise.reject(new Error("x")));
    });
    expect(h.result.current.pending).toBe(true);
    await act(async () => {
      await expect(p).rejects.toThrow("x");
    });
    expect(h.result.current.pending).toBe(false);
    expect(h.result.current.locked).toBe(false);
  });

  it("a fn throwing synchronously clears pending", async () => {
    const h = renderHook(() => useWriteLock());
    let p!: Promise<boolean>;
    act(() => {
      p = h.result.current.runWrite(() => {
        throw new Error("sync");
      });
    });
    await act(async () => {
      await expect(p).rejects.toThrow("sync");
    });
    expect(h.result.current.pending).toBe(false);
  });

  it("a refused runWrite never calls fn and changes nothing", async () => {
    const h = renderHook(() => useWriteLock());
    act(() => {
      h.result.current.tryOpenDialog();
    });
    const fn = vi.fn(async () => {});
    let ok: boolean | undefined;
    await act(async () => {
      ok = await h.result.current.runWrite(fn);
    });
    expect(ok).toBe(false);
    expect(fn).not.toHaveBeenCalled();
    expect(h.result.current.pending).toBe(false);
    expect(h.result.current.locked).toBe(true);
  });
});

describe("useWriteLock: isFree", () => {
  for (const start of STARTS) {
    it(`isFree in state ${start}`, () => {
      const h = renderHook(() => useWriteLock());
      const gate = held();
      const s = startState(h, start, gate);
      expect(h.result.current.isFree()).toBe(!s.pending && !s.dialog);
      gate.resolve();
    });
  }
});

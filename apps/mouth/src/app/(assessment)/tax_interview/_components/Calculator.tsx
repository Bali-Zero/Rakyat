"use client";

import { useState } from "react";
import styles from "../tax-interview.module.css";

type Op = "+" | "-" | "×" | "÷";
const KEYS = [
  "C",
  "⌫",
  "÷",
  "×",
  "7",
  "8",
  "9",
  "-",
  "4",
  "5",
  "6",
  "+",
  "1",
  "2",
  "3",
  ",",
];

const apply = (a: number, b: number, op: Op): number =>
  op === "+"
    ? a + b
    : op === "-"
      ? a - b
      : op === "×"
        ? a * b
        : b === 0
          ? NaN
          : a / b;

const show = (s: string): string => {
  if (s === "Error") return s;
  const [int, dec] = s.split(",");
  return `${Number(int || "0").toLocaleString("id-ID")}${dec !== undefined ? `,${dec}` : ""}`.replace(
    /^-0$/,
    "0",
  );
};

/** One plain four-function calculator: the test allows an on-screen one, nothing richer. */
export function Calculator({ onClose }: { onClose: () => void }) {
  const [cur, setCur] = useState("0");
  const [acc, setAcc] = useState<number | null>(null);
  const [op, setOp] = useState<Op | null>(null);
  const [fresh, setFresh] = useState(false);
  const value = () => Number(cur.replace(",", "."));
  const out = (n: number) =>
    Number.isFinite(n)
      ? String(Math.round(n * 1e8) / 1e8).replace(".", ",")
      : "Error";

  function press(k: string) {
    if (k === "C")
      return (setCur("0"), setAcc(null), setOp(null), setFresh(false));
    if (cur === "Error") setCur("0");
    if (/\d/.test(k)) {
      setCur((c) =>
        fresh || c === "0" || c === "Error" ? k : c.length < 15 ? c + k : c,
      );
      return setFresh(false);
    }
    if (k === ",") {
      setCur((c) => (fresh ? "0," : c.includes(",") ? c : `${c},`));
      return setFresh(false);
    }
    if (k === "⌫")
      return setCur((c) =>
        fresh || c.length <= 1 || c === "Error" ? "0" : c.slice(0, -1),
      );
    const result =
      acc !== null && op && !fresh ? apply(acc, value(), op) : value();
    if (k === "=") {
      setCur(out(result));
      setAcc(null);
      setOp(null);
      return setFresh(true);
    }
    setAcc(result);
    setCur(out(result));
    setOp(k as Op);
    setFresh(true);
  }

  return (
    <div className={styles.calc} role="dialog" aria-label="Kalkulator">
      <div className={styles.calcHead}>
        <span>Kalkulator</span>
        <button
          type="button"
          className={styles.iconBtn}
          onClick={onClose}
          aria-label="Tutup kalkulator"
        >
          ×
        </button>
      </div>
      <div className={styles.calcScreen} aria-live="polite">
        {show(cur)}
      </div>
      <div className={styles.calcKeys}>
        {KEYS.map((k) => (
          <button
            key={k}
            type="button"
            className={`${styles.key} ${"C⌫÷×-+".includes(k) ? styles.keyOp : ""}`}
            onClick={() => press(k)}
          >
            {k}
          </button>
        ))}
        <button
          type="button"
          className={styles.key}
          style={{ gridColumn: "span 2" }}
          onClick={() => press("0")}
        >
          0
        </button>
        <button
          type="button"
          className={`${styles.key} ${styles.keyEq}`}
          style={{ gridColumn: "span 2" }}
          onClick={() => press("=")}
        >
          =
        </button>
      </div>
    </div>
  );
}

"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import styles from "../tax-interview.module.css";

const MESSAGES: Record<number, string> = {
  401: "PIN salah. Coba lagi.",
  429: "Terlalu banyak percobaan. Tunggu beberapa menit, lalu coba lagi.",
  503: "Halaman belum siap. Hubungi pewawancara.",
};

/** The only thing an unauthenticated visitor ever receives from this route. */
export function PinGate() {
  const router = useRouter();
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || !pin.trim()) return;
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/tax-interview/unlock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin }),
      });
      if (res.ok) {
        router.refresh();
        return;
      }
      setError(
        MESSAGES[res.status] ??
          "Terjadi kendala. Coba lagi atau hubungi pewawancara.",
      );
    } catch {
      setError("Koneksi bermasalah. Coba lagi.");
    }
    setBusy(false);
  }

  return (
    <div className={`${styles.page} ${styles.center}`}>
      <form className={styles.card} onSubmit={submit} noValidate>
        <p className={styles.eyebrow}>Bali Zero</p>
        <h1 className={styles.h1}>Akses terbatas</h1>
        <p className={styles.lead}>
          Masukkan PIN yang diberikan pewawancara untuk membuka halaman ini.
        </p>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="pin">
            PIN
          </label>
          <input
            id="pin"
            className={styles.input}
            type="password"
            inputMode="numeric"
            autoComplete="off"
            autoFocus
            maxLength={12}
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            aria-describedby={error ? "pin-error" : undefined}
          />
        </div>
        {error && (
          <p id="pin-error" role="alert" className={styles.error}>
            {error}
          </p>
        )}
        <button
          className={styles.btn}
          type="submit"
          disabled={busy || !pin.trim()}
        >
          {busy ? "Memeriksa..." : "Buka"}
        </button>
      </form>
    </div>
  );
}

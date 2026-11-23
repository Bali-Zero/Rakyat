"use client";

import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  acceptResponse,
  isSameIdentity,
  useSessionIdentity,
  type SessionIdentity,
} from "@/lib/auth/session-identity";

type Item = {
  practice_id: number;
  service: string;
  employee_name: string;
  earned_at: string;
  amount_idr: number | null;
};
type Reviews = { viewer_team_member_id: string; items: Item[] };

function Review({ item, onResolved }: { item: Item; onResolved: () => void }) {
  const [amount, setAmount] = useState(String(item.amount_idr ?? ""));
  const [message, setMessage] = useState("");
  const [carry, setCarry] = useState(false);
  const [busy, setBusy] = useState(false);
  async function resolve(event: React.FormEvent) {
    event.preventDefault();
    if (busy || amount.trim() === "") return;
    const rate = Number(amount);
    if (!Number.isSafeInteger(rate) || rate < 0 || rate > 100000000) return;
    setBusy(true);
    setMessage("");
    try {
      await api.post(`/api/hr/delivery-bonus-reviews/${item.practice_id}`, {
        amount_idr: rate,
        use_current_period: carry,
      });
      onResolved();
    } catch (e) {
      setMessage(
        e instanceof Error ? e.message : "Could not record this bonus",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      onSubmit={resolve}
      className="flex flex-wrap items-end gap-3 border-t border-[var(--bz-border)] py-2 text-sm"
    >
      <div className="flex-1">
        <strong>{item.employee_name}</strong>
        <p>
          {item.service} · #{item.practice_id} ·{" "}
          {new Date(item.earned_at).toLocaleDateString("en-GB", {
            timeZone: "Asia/Makassar",
          })}
        </p>
      </div>
      <label>
        Bonus (IDR)
        <input
          className="block w-32 rounded border bg-[var(--bz-surface)] px-2 py-1"
          type="number"
          step="1"
          min="0"
          max="100000000"
          required
          readOnly={item.amount_idr !== null}
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
        />
      </label>
      <label className="flex gap-1">
        <input
          type="checkbox"
          checked={carry}
          onChange={(e) => setCarry(e.target.checked)}
        />{" "}
        Move a locked award to this month
      </label>
      <button
        className="rounded border px-2 py-1"
        type="submit"
        disabled={busy}
      >
        Record pending bonus
      </button>
      {message && <p role="alert">{message}</p>}
    </form>
  );
}
export function DeliveryReviews({ onResolved }: { onResolved: () => void }) {
  const identity = useSessionIdentity();
  const [bound, setBound] = useState<{
    identity: SessionIdentity;
    items: Item[];
  } | null>(null);
  const [seq, setSeq] = useState(0);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    setFailed(false);
    if (identity)
      api
        .requestWithCredential<Reviews>("/api/hr/delivery-bonus-reviews")
        .then((res) => {
          if (
            live &&
            acceptResponse(res, identity, res.data.viewer_team_member_id)
          )
            setBound({ identity, items: res.data.items });
        })
        .catch(() => {
          if (live) setFailed(true);
        });
    return () => {
      live = false;
    };
  }, [identity, seq]);
  if (!identity) return null;
  if (failed)
    return (
      <button
        onClick={() => setSeq((s) => s + 1)}
        className="text-sm underline"
      >
        Reload delivery bonus reviews
      </button>
    );
  if (
    !bound ||
    !isSameIdentity(identity, bound.identity) ||
    !bound.items.length
  )
    return null;
  return (
    <section className="rounded border border-[var(--bz-border)] p-4">
      <h2 className="font-semibold">Delivery bonuses awaiting HR</h2>
      {bound.items.map((item) => (
        <Review
          key={`${identity.epoch}-${identity.sub}-${item.practice_id}`}
          item={item}
          onResolved={() => {
            setSeq((s) => s + 1);
            onResolved();
            window.dispatchEvent(new Event("kita:delivery-changed"));
          }}
        />
      ))}
    </section>
  );
}

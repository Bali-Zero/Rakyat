"use client";

import {
  useRef,
  useState,
  type ClipboardEvent,
  type CompositionEvent,
  type FocusEvent,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { countWords } from "@/app/(assessment)/interview_natalie/telemetry";
import type { Answers, Widget } from "../types";
import styles from "../tax-interview.module.css";

export interface FieldHooks {
  handlers: {
    onFocus: (e: FocusEvent) => void;
    onKeyDown: (e: KeyboardEvent) => void;
    onPaste: (e: ClipboardEvent) => void;
    onCut: (e: ClipboardEvent) => void;
    onCopy: () => void;
    onContextMenu: (e: MouseEvent) => void;
    onCompositionStart: (e: CompositionEvent) => void;
    onCompositionEnd: (e: CompositionEvent) => void;
  };
  onValue: (prev: string, next: string) => void;
}
export type Tele = (field: string) => FieldHooks;

interface P<K extends Widget["kind"]> {
  w: Extract<Widget, { kind: K }>;
  a: Answers;
  up: (f: (a: Answers) => Answers) => void;
  tele: Tele;
  seed: string;
}

/** Deterministic (mulberry32) so a reload shows the SAME order the candidate already saw. */
export function shuffled<T>(arr: T[], seed: string): T[] {
  let h = 2166136261;
  for (const c of seed) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  const rnd = () => {
    let t = (h += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const out = [...arr];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

function Text({
  id,
  value,
  onChange,
  tele,
  max,
  lang,
  rows,
  label,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  tele: Tele;
  max: number;
  lang?: string;
  rows?: number;
  label: string;
}) {
  const hooks = tele(id);
  const n = countWords(value);
  return (
    <div>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <textarea
        id={id}
        className={styles.textarea}
        rows={rows ?? 6}
        lang={lang}
        value={value}
        {...hooks.handlers}
        onChange={(e) => {
          hooks.onValue(value, e.target.value);
          onChange(e.target.value);
        }}
      />
      <div className={`${styles.counter} ${n > max ? styles.counterOver : ""}`}>
        {n} / {max} kata
      </div>
    </div>
  );
}

function Choice({
  name,
  value,
  options,
  onPick,
  kind = "radio",
}: {
  name: string;
  value: string | string[];
  options: { id: string; label: string }[];
  onPick: (id: string) => void;
  kind?: "radio" | "checkbox";
}) {
  const on = (id: string) =>
    Array.isArray(value) ? value.includes(id) : value === id;
  return (
    <div
      className={styles.choices}
      role={kind === "radio" ? "radiogroup" : "group"}
    >
      {options.map((o) => (
        <label
          key={o.id}
          className={`${styles.choice} ${on(o.id) ? styles.choiceOn : ""}`}
        >
          <input
            type={kind}
            name={name}
            checked={on(o.id)}
            onChange={() => onPick(o.id)}
          />
          <span>{o.label}</span>
        </label>
      ))}
    </div>
  );
}

export function SortWidget({ w, a, up }: P<"sort">) {
  const order = a.m1 ?? w.cards.map((c) => c.id);
  const label = new Map(w.cards.map((c) => [c.id, c.label]));
  const refs = useRef(new Map<string, HTMLLIElement>());
  const [drag, setDrag] = useState<string | null>(null);
  const set = (o: string[]) => up((x) => ({ ...x, m1: o }));
  const move = (id: string, to: number) => {
    const rest = order.filter((i) => i !== id);
    rest.splice(Math.max(0, Math.min(to, rest.length)), 0, id);
    if (rest.join() !== order.join()) set(rest);
  };
  const dragTo = (y: number) => {
    if (!drag) return;
    let target = 0;
    for (const id of order) {
      const r =
        id === drag ? null : refs.current.get(id)?.getBoundingClientRect();
      if (r && r.top + r.height / 2 < y) target++;
    }
    move(drag, target);
  };
  return (
    <div className={styles.work}>
      <p className={styles.workHead}>
        Seret kartu, atau pakai tombol panah, dari yang paling mendesak (atas)
        ke yang paling santai (bawah).
      </p>
      <p className={styles.sortEnds}>
        <span>Paling mendesak</span>
        <span>Paling santai</span>
      </p>
      <ol className={styles.sortList}>
        {order.map((id, i) => (
          <li
            key={id}
            ref={(el) => {
              if (el) refs.current.set(id, el);
            }}
            className={`${styles.sortItem} ${drag === id ? styles.sortItemLifted : ""}`}
          >
            <span className={styles.rank} aria-hidden>
              {i + 1}
            </span>
            <button
              type="button"
              className={`${styles.iconBtn} ${styles.handle}`}
              aria-label={`Geser kartu ${i + 1}`}
              onPointerDown={(e) => {
                e.currentTarget.setPointerCapture(e.pointerId);
                setDrag(id);
              }}
              onPointerMove={(e) => dragTo(e.clientY)}
              onPointerUp={() => setDrag(null)}
              onPointerCancel={() => setDrag(null)}
              onKeyDown={(e) => {
                if (e.key === "ArrowUp") (e.preventDefault(), move(id, i - 1));
                if (e.key === "ArrowDown")
                  (e.preventDefault(), move(id, i + 1));
              }}
            >
              ⠿
            </button>
            <span className={styles.sortText}>{label.get(id)}</span>
            <button
              type="button"
              className={`${styles.iconBtn} ${styles.up}`}
              aria-label={`Naikkan kartu ${i + 1}`}
              disabled={i === 0}
              onClick={() => move(id, i - 1)}
            >
              ↑
            </button>
            <button
              type="button"
              className={`${styles.iconBtn} ${styles.down}`}
              aria-label={`Turunkan kartu ${i + 1}`}
              disabled={i === order.length - 1}
              onClick={() => move(id, i + 1)}
            >
              ↓
            </button>
          </li>
        ))}
      </ol>
      {a.m1 === null && (
        <div>
          <button
            type="button"
            className={styles.btnGhost}
            onClick={() => set(order)}
          >
            Pakai urutan ini sebagai jawabanku
          </button>
        </div>
      )}
    </div>
  );
}

export function MatchWidget({ w, a, up }: P<"match">) {
  return (
    <div className={styles.work}>
      {w.parts.map((p) => (
        <div key={p.title} className={styles.rows}>
          <h3 className={styles.sub}>{p.title}</h3>
          <p className={styles.noteLine}>{p.note}</p>
          {p.rows.map((r) => (
            <div key={r.id} className={styles.matchRow}>
              <label className={styles.rowLabel} htmlFor={`m2-${r.id}`}>
                {r.label}
              </label>
              <select
                id={`m2-${r.id}`}
                className={styles.select}
                value={a.m2[r.id] ?? ""}
                onChange={(e) =>
                  up((x) => ({ ...x, m2: { ...x.m2, [r.id]: e.target.value } }))
                }
              >
                <option value="">Pilih...</option>
                {p.options.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export function HotspotWidget({ w, a, up }: P<"hotspot">) {
  const toggle = (id: string) =>
    up((x) => ({
      ...x,
      m3: x.m3.includes(id) ? x.m3.filter((i) => i !== id) : [...x.m3, id],
    }));
  return (
    <div className={styles.work}>
      <p className={styles.workHead}>
        Ketuk kolom yang menurutmu salah. Ketuk lagi untuk membatalkan. Boleh
        lebih dari satu.
      </p>
      <div className={styles.split}>
        <section className={styles.paper} aria-label={w.doc.title}>
          <h3 className={styles.paperTitle}>{w.doc.title}</h3>
          <div className={styles.hotGrid}>
            {w.doc.fields.map((f) => (
              <button
                key={f.id}
                type="button"
                className={styles.hot}
                aria-pressed={a.m3.includes(f.id)}
                onClick={() => toggle(f.id)}
              >
                <span>{f.label}</span>
                <span>{f.value}</span>
              </button>
            ))}
          </div>
        </section>
        <section
          className={`${styles.paper} ${styles.paperRef}`}
          aria-label={w.invoice.title}
        >
          <h3 className={styles.paperTitle}>{w.invoice.title}</h3>
          <dl className={styles.dl}>
            {w.invoice.lines.map((l) => (
              <div key={l.label}>
                <dt>{l.label}</dt>
                <dd>{l.value}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </div>
  );
}

export function MathWidget({ w, a, up }: P<"math">) {
  return (
    <div className={styles.work}>
      <p className={styles.workHead}>
        Kalkulator di layar boleh dipakai. Tulis angka dalam Rupiah; titik atau
        koma boleh, boleh juga tanpa keduanya.
      </p>
      {w.problems.map((p) => (
        <div key={p.id} className={styles.problem}>
          <h3 className={styles.sub}>{p.title}</h3>
          <p className={styles.problemText}>{p.text}</p>
          <div className={styles.inputs}>
            {p.inputs.map((i) => (
              <div key={i.id} className={styles.field} style={{ margin: 0 }}>
                <label className={styles.label} htmlFor={`m4-${i.id}`}>
                  {i.label}
                </label>
                <div className={styles.rp}>
                  <input
                    id={`m4-${i.id}`}
                    className={styles.input}
                    type="text"
                    inputMode="decimal"
                    autoComplete="off"
                    value={a.m4[i.id] ?? ""}
                    onChange={(e) =>
                      up((x) => ({
                        ...x,
                        m4: { ...x.m4, [i.id]: e.target.value },
                      }))
                    }
                  />
                </div>
              </div>
            ))}
          </div>
          {p.choice && (
            <div>
              <p className={styles.problemText} style={{ marginBottom: 8 }}>
                <strong>{p.choice.text}</strong>
              </p>
              <Choice
                name={p.choice.id}
                value={a.m4[p.choice.id] ?? ""}
                options={p.choice.options}
                onPick={(id) =>
                  up((x) => ({ ...x, m4: { ...x.m4, [p.choice!.id]: id } }))
                }
              />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function ReconWidget({ w, a, up }: P<"recon">) {
  const set = (id: string, v: string) =>
    up((x) => ({ ...x, m5: { ...x.m5, [id]: v } }));
  return (
    <div className={styles.work}>
      <details className={styles.ref} open>
        <summary>Daftar invoice penjualan (PPN efektif 11%)</summary>
        <div className={styles.scroller}>
          <table className={styles.tbl}>
            <thead>
              <tr>
                {w.invoices[0].map((h, i) => (
                  <th key={h} className={i > 1 ? styles.num : undefined}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {w.invoices.slice(1).map((r) => (
                <tr key={r[0]}>
                  {r.map((c, i) => (
                    <td key={i} className={i > 1 ? styles.num : undefined}>
                      {c}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <div>
        <h3 className={styles.sub}>Rekening koran Oktober</h3>
        <p className={styles.noteLine}>
          Tandai status tiap mutasi. &quot;Dicatat sebagai&quot; adalah yang
          sudah dicatat di buku.
        </p>
      </div>
      <div className={styles.scroller}>
        <table className={`${styles.tbl} ${styles.reconTbl}`}>
          <thead>
            <tr>
              <th>Tgl</th>
              <th>Keterangan bank</th>
              <th>D/K</th>
              <th className={styles.num}>Jumlah</th>
              <th>Dicatat sebagai</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {w.rows.map((r) => (
              <tr key={r.id}>
                <td data-label="Tgl">{r.date}</td>
                <td className={styles.memo}>{r.memo}</td>
                <td data-label="D/K">{r.dk}</td>
                <td className={styles.num} data-label="Jumlah">
                  {r.amount}
                </td>
                <td data-label="Dicatat sebagai">{r.booked}</td>
                <td>
                  <select
                    className={styles.select}
                    aria-label={`Status mutasi ${r.memo} tanggal ${r.date}, ${r.dk} ${r.amount}`}
                    value={a.m5[r.id] ?? ""}
                    onChange={(e) => set(r.id, e.target.value)}
                  >
                    <option value="">Pilih status...</option>
                    {w.statuses.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {w.questions.map((q) => (
        <div key={q.id} className={styles.problem}>
          <p className={styles.problemText}>
            <strong>{q.text}</strong>
          </p>
          <Choice
            name={q.id}
            value={a.m5[q.id] ?? ""}
            options={q.options}
            onPick={(id) => set(q.id, id)}
          />
        </div>
      ))}
    </div>
  );
}

export function PortalWidget({ w, a, up, tele, seed }: P<"portal">) {
  const options = shuffled(w.options, seed);
  const toggle = (id: string) =>
    up((x) => ({
      ...x,
      m6: {
        ...x.m6,
        checked: x.m6.checked.includes(id)
          ? x.m6.checked.filter((i) => i !== id)
          : [...x.m6.checked, id],
      },
    }));
  return (
    <div className={styles.work}>
      <div>
        <h3 className={styles.sub}>{w.pick}</h3>
        <p className={styles.noteLine}>Centang semua yang kamu lakukan.</p>
      </div>
      <Choice
        kind="checkbox"
        name="m6"
        value={a.m6.checked}
        options={options}
        onPick={toggle}
      />
      <Text
        id="m6-note"
        label={w.prompt}
        value={a.m6.note}
        max={w.maxWords}
        rows={5}
        tele={tele}
        onChange={(v) => up((x) => ({ ...x, m6: { ...x.m6, note: v } }))}
      />
    </div>
  );
}

export function ReplyWidget({ w, a, up, tele }: P<"reply">) {
  return (
    <div className={styles.work}>
      <div className={styles.info}>
        <strong>{w.infoTitle}</strong>
        {w.info}
      </div>
      <Text
        id="m7-reply"
        label="Balasan WhatsApp untuk klien (dalam bahasa Inggris)"
        value={a.m7.reply}
        max={w.maxWords}
        lang="en"
        tele={tele}
        onChange={(v) => up((x) => ({ ...x, m7: { reply: v } }))}
      />
    </div>
  );
}

export function IntegrityWidget({ w, a, up, tele, seed }: P<"integrity">) {
  const cur = (id: string) => a.m8[id] ?? { choice: "", reason: "" };
  const put = (
    id: string,
    patch: Partial<{ choice: string; reason: string }>,
  ) =>
    up((x) => ({
      ...x,
      m8: {
        ...x.m8,
        [id]: { ...(x.m8[id] ?? { choice: "", reason: "" }), ...patch },
      },
    }));
  return (
    <div className={styles.work}>
      <p className={styles.workHead}>
        Empat situasi singkat. Pilih satu jawaban, lalu tulis alasanmu dalam
        satu atau dua kalimat.
      </p>
      {w.situations.map((s) => (
        <div key={s.id} className={styles.sit}>
          <div>
            <h3 className={styles.sub}>{s.title}</h3>
            <p className={styles.sitText}>{s.text}</p>
          </div>
          <Choice
            name={`m8-${s.id}`}
            value={cur(s.id).choice}
            options={shuffled(s.options, `${seed}${s.id}`)}
            onPick={(id) => put(s.id, { choice: id })}
          />
          <Text
            id={`m8-${s.id}`}
            label="Alasanmu (maksimal 25 kata)"
            value={cur(s.id).reason}
            max={w.maxWords}
            rows={2}
            tele={tele}
            onChange={(v) => put(s.id, { reason: v })}
          />
        </div>
      ))}
    </div>
  );
}

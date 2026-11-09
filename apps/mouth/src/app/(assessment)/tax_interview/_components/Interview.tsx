"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  JUMP_THRESHOLD,
  countWords,
  emptyField,
  type ExerciseTelemetry,
} from "@/app/(assessment)/interview_natalie/telemetry";
import {
  MISSION_IDS,
  emptyAnswers,
  type Answers,
  type ChatMsg,
  type Content,
  type Mission,
  type SubmitPayload,
} from "../types";
import styles from "../tax-interview.module.css";
import { Calculator } from "./Calculator";
import {
  HotspotWidget,
  IntegrityWidget,
  MatchWidget,
  MathWidget,
  PortalWidget,
  ReconWidget,
  ReplyWidget,
  SortWidget,
  type Tele,
} from "./widgets";

const STORAGE_KEY = "bz-tax-interview-v1";
const DONE_KEY = "bz-tax-interview-v1-done";
const TARGET_SECONDS = 45 * 60;
const DAY_MINUTES = 8 * 60 + 30;

interface Session {
  attemptId: string;
  name: string;
  startedAt: number;
  idx: number;
  answers: Answers;
}
interface Stored extends Session {
  v: 1;
  telem: Record<string, ExerciseTelemetry>;
}
type Phase = "boot" | "start" | "run" | "done";
type SendError = "" | "send" | "auth";

const newTelem = (): Record<string, ExerciseTelemetry> =>
  Object.fromEntries(
    MISSION_IDS.map((id) => [
      id,
      {
        elapsedMs: 0,
        awayMs: 0,
        awayCount: 0,
        fields: {},
        autoLocked: false,
        restored: false,
        composedChars: 0,
      },
    ]),
  );

const uuid = (): string =>
  typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
        (
          Number(c) ^
          (crypto.getRandomValues(new Uint8Array(1))[0] &
            (15 >> (Number(c) / 4)))
        ).toString(16),
      );

const read = (): Stored | null => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const s = raw ? (JSON.parse(raw) as Stored) : null;
    return s && s.v === 1 && s.attemptId && s.answers ? s : null;
  } catch {
    return null;
  }
};
const mmss = (s: number): string =>
  `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
const minutesOf = (t: string): number => {
  const [h, m] = t.split(".").map(Number);
  return h * 60 + m - DAY_MINUTES;
};

function answered(id: string, a: Answers): boolean {
  switch (id) {
    case "m1_triage":
      return a.m1 !== null;
    case "m2_calendar":
      return Object.values(a.m2).some(Boolean);
    case "m3_hotspots":
      return a.m3.length > 0;
    case "m4_math":
      return Object.values(a.m4).some((v) => v.trim());
    case "m5_recon":
      return Object.values(a.m5).some(Boolean);
    case "m6_portal_down":
      return a.m6.checked.length > 0 || !!a.m6.note.trim();
    case "m7_english_reply":
      return !!a.m7.reply.trim();
    default:
      return Object.values(a.m8).some((e) => e.choice || e.reason.trim());
  }
}

function Bubble({
  msg,
  content,
  i,
}: {
  msg: ChatMsg;
  content: Content;
  i: number;
}) {
  const p = content.personas[msg.from];
  return (
    <div className={styles.msg} style={{ ["--i" as string]: i }}>
      <div
        className={`${styles.avatar} ${styles[`av_${msg.from}`]}`}
        aria-hidden
      >
        {p.initials}
      </div>
      <div>
        <p className={styles.who}>
          <strong>{p.name}</strong>
          {msg.at}
        </p>
        <div className={styles.bubble} lang={msg.en ? "en" : undefined}>
          {msg.text}
          {msg.attachment && (
            <div
              className={`${styles.attach} ${msg.attachment.variant === "error" ? styles.attachError : styles.attachLetter}`}
            >
              <strong>{msg.attachment.title}</strong>
              {msg.attachment.lines.map((l) => (
                <div key={l}>{l}</div>
              ))}
              {msg.attachment.foot && <small>{msg.attachment.foot}</small>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function Interview({ content }: { content: Content }) {
  const [phase, setPhase] = useState<Phase>("boot");
  const [stored, setStored] = useState<Stored | null>(null);
  const [name, setName] = useState("");
  const [session, setSession] = useState<Session | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [calc, setCalc] = useState(false);
  const [toast, setToast] = useState("");
  const [confirmSend, setConfirmSend] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<SendError>("");
  const [askReset, setAskReset] = useState(false);
  const [dirty, setDirty] = useState(false);

  const telem = useRef(newTelem());
  const idxRef = useRef(0);
  const lastTick = useRef(0);
  const awaySince = useRef<number | null>(null);
  const focusedAt = useRef<Record<string, number>>({});
  const lastEdit = useRef<Record<string, number>>({});
  const composing = useRef<Record<string, boolean>>({});
  const endedAt = useRef<number | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    // Storage is only readable in the browser; reading it in render would break hydration.
    if (localStorage.getItem(DONE_KEY)) setPhase("done");
    else {
      setStored(read());
      setPhase("start");
    }
  }, []);

  const flush = useCallback(() => {
    const t = Date.now();
    const m = telem.current[MISSION_IDS[idxRef.current]];
    m.elapsedMs += t - lastTick.current;
    lastTick.current = t;
  }, []);

  const persist = useCallback(
    (s: Session | null) => {
      if (!s) return;
      flush();
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({ ...s, v: 1, telem: telem.current } satisfies Stored),
        );
      } catch {
        // Private mode or a full quota: the page still works, it just cannot resume.
      }
    },
    [flush],
  );

  useEffect(() => {
    if (phase !== "run") return;
    persist(session);
    const id = window.setInterval(() => {
      setNow(Date.now());
      persist(session);
    }, 1000);
    const hide = () => persist(session);
    window.addEventListener("pagehide", hide);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("pagehide", hide);
    };
  }, [phase, session, persist]);

  useEffect(() => {
    if (phase !== "run") return;
    const leave = () => {
      if (awaySince.current === null) {
        awaySince.current = Date.now();
        telem.current[MISSION_IDS[idxRef.current]].awayCount += 1;
      }
    };
    const back = () => {
      if (awaySince.current !== null) {
        telem.current[MISSION_IDS[idxRef.current]].awayMs +=
          Date.now() - awaySince.current;
        awaySince.current = null;
      }
    };
    const vis = () => (document.hidden ? leave() : back());
    window.addEventListener("blur", leave);
    window.addEventListener("focus", back);
    document.addEventListener("visibilitychange", vis);
    return () => {
      window.removeEventListener("blur", leave);
      window.removeEventListener("focus", back);
      document.removeEventListener("visibilitychange", vis);
    };
  }, [phase]);

  const say = useCallback((m: string) => {
    setToast(m);
    window.setTimeout(() => setToast((t) => (t === m ? "" : t)), 4000);
  }, []);

  const update = useCallback((f: (a: Answers) => Answers) => {
    setSession((s) => (s ? { ...s, answers: f(s.answers) } : s));
    setDirty(true);
  }, []);

  const tele: Tele = (id) => {
    const field = () =>
      (telem.current[MISSION_IDS[idxRef.current]].fields[id] ??= emptyField());
    return {
      handlers: {
        onFocus: () => void (focusedAt.current[id] ??= Date.now()),
        onKeyDown: (e) => {
          const t = field();
          if (e.key === "Backspace" || e.key === "Delete") t.backspaces += 1;
          else if (e.key === "Process" || e.key === "Unidentified")
            t.keystrokes += 1;
          else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
            t.keystrokes += 1;
            t.timeToFirstKeyMs ??=
              Date.now() - (focusedAt.current[id] ?? Date.now());
          }
        },
        onPaste: (e) => {
          e.preventDefault();
          const t = field();
          t.pasteAttempts += 1;
          t.pastedChars += (e.clipboardData?.getData("text") || "").length;
          say(
            "Menempel (paste) dinonaktifkan di tes ini. Silakan ketik jawabanmu.",
          );
        },
        onCut: (e) => {
          e.preventDefault();
          field().cutEvents += 1;
          say("Memotong (cut) dinonaktifkan karena menempel juga tidak bisa.");
        },
        onCopy: () => void (field().copyEvents += 1),
        onContextMenu: () => void (field().contextMenus += 1),
        onCompositionStart: () => void (composing.current[id] = true),
        onCompositionEnd: () => void (composing.current[id] = false),
      },
      onValue: (prev, next) => {
        const t = field();
        const delta = next.length - prev.length;
        if (composing.current[id]) {
          if (delta > 0)
            telem.current[MISSION_IDS[idxRef.current]].composedChars += delta;
        } else if (delta > JUMP_THRESHOLD) {
          t.jumpInsertions += 1;
          t.jumpChars += delta;
        }
        const n = Date.now();
        if (lastEdit.current[id])
          t.maxIdleMs = Math.max(t.maxIdleMs, n - lastEdit.current[id]);
        lastEdit.current[id] = n;
        t.chars = next.length;
        t.words = countWords(next);
      },
    };
  };

  function begin(s: Session, t: Record<string, ExerciseTelemetry>) {
    telem.current = t;
    idxRef.current = s.idx;
    lastTick.current = Date.now();
    awaySince.current = null;
    endedAt.current = null;
    setSession(s);
    setNow(Date.now());
    setPhase("run");
  }

  function startNew() {
    const s: Session = {
      attemptId: uuid(),
      name: name.trim(),
      startedAt: Date.now(),
      idx: 0,
      answers: emptyAnswers(),
    };
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {}
    begin(s, newTelem());
  }

  function resume() {
    if (!stored) return;
    const t = newTelem();
    for (const id of MISSION_IDS) {
      const saved = stored.telem?.[id];
      if (saved) t[id] = { ...t[id], ...saved };
    }
    const a = stored.answers;
    // Typed text survives a reload but the keystrokes that made it do not: tell the panel.
    if (a.m6.note.trim()) t.m6_portal_down.restored = true;
    if (a.m7.reply.trim()) t.m7_english_reply.restored = true;
    if (Object.values(a.m8).some((e) => e.reason.trim()))
      t.m8_integrity.restored = true;
    begin(
      {
        attemptId: stored.attemptId,
        name: stored.name,
        startedAt: stored.startedAt,
        idx: stored.idx,
        answers: { ...emptyAnswers(), ...a },
      },
      t,
    );
  }

  function discard() {
    try {
      localStorage.removeItem(STORAGE_KEY);
      localStorage.removeItem(DONE_KEY);
    } catch {}
    setStored(null);
    setAskReset(false);
    setSession(null);
    setName("");
    setPhase("start");
  }

  function go(i: number) {
    const next = Math.max(0, Math.min(i, content.missions.length - 1));
    if (!session || next === session.idx) return;
    flush();
    if (awaySince.current !== null) {
      telem.current[MISSION_IDS[session.idx]].awayMs +=
        Date.now() - awaySince.current;
      awaySince.current = Date.now();
    }
    idxRef.current = next;
    setConfirmSend(false);
    setSession({ ...session, idx: next });
    window.scrollTo({ top: 0 });
    window.setTimeout(() => heading.current?.focus({ preventScroll: true }), 0);
  }

  async function send() {
    if (!session || sending) return;
    setSending(true);
    setSendError("");
    flush();
    if (awaySince.current !== null) {
      telem.current[MISSION_IDS[session.idx]].awayMs +=
        Date.now() - awaySince.current;
      awaySince.current = Date.now();
    }
    endedAt.current ??= Date.now();
    const payload: SubmitPayload = {
      attemptId: session.attemptId,
      name: session.name,
      startedAt: session.startedAt,
      endedAt: endedAt.current,
      answers: session.answers,
      telemetry: telem.current,
    };
    try {
      const res = await fetch("/api/tax-interview/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(25_000),
      });
      if (res.ok) {
        try {
          localStorage.removeItem(STORAGE_KEY);
          localStorage.setItem(DONE_KEY, "1");
        } catch {}
        setPhase("done");
        window.scrollTo({ top: 0 });
        return;
      }
      setSendError(res.status === 401 ? "auth" : "send");
    } catch {
      setSendError("send");
    }
    setSending(false);
  }

  /* ── Screens ── */
  if (phase === "boot") return <div className={styles.page} aria-busy="true" />;

  if (phase === "done")
    return (
      <div className={`${styles.page} ${styles.center}`}>
        <div className={styles.closing}>
          <Bubble msg={content.closing} content={content} i={0} />
          <p className={styles.lead}>
            Jawabanmu sudah terkirim. Kamu boleh menutup halaman ini.
          </p>
          {askReset ? (
            <div className={styles.actions}>
              <button type="button" className={styles.btn} onClick={discard}>
                Ya, reset perangkat ini
              </button>
              <button
                type="button"
                className={styles.btnGhost}
                onClick={() => setAskReset(false)}
              >
                Batal
              </button>
            </div>
          ) : (
            <div>
              <button
                type="button"
                className={styles.linkBtn}
                onClick={() => setAskReset(true)}
              >
                Reset perangkat ini untuk kandidat berikutnya
              </button>
            </div>
          )}
        </div>
      </div>
    );

  if (phase === "start")
    return (
      <div className={`${styles.page} ${styles.center}`}>
        <div className={styles.card}>
          <p className={styles.eyebrow}>Tes tertulis, sekitar 45 menit</p>
          <h1 className={styles.h1}>{content.title}</h1>
          <p className={styles.lead}>{content.subtitle}</p>
          <ul className={styles.notice}>
            <li>
              Yang dicatat: waktu di setiap misi, kejadian menempel (paste) dan
              perpindahan tab.
            </li>
            <li>
              Kalkulator di layar boleh dipakai (tombol Kalkulator di bagian
              atas).
            </li>
            <li>
              Menempel teks dinonaktifkan di kolom tulisan; silakan mengetik
              sendiri.
            </li>
            <li>
              Jawabanmu tersimpan otomatis di perangkat ini. Jika halaman dimuat
              ulang, kamu bisa melanjutkan.
            </li>
            <li>
              Tidak ada skor atau penilaian yang tampil di layar. Hasilnya
              dibahas bersama panel.
            </li>
          </ul>
          {stored ? (
            <div className={styles.field}>
              <p className={styles.lead} style={{ margin: 0 }}>
                Ada sesi yang tersimpan di perangkat ini atas nama{" "}
                <strong>{stored.name}</strong>.
              </p>
              <div className={styles.actions}>
                <button type="button" className={styles.btn} onClick={resume}>
                  Lanjutkan sesi ini
                </button>
                {askReset ? (
                  <button
                    type="button"
                    className={styles.btnGhost}
                    onClick={discard}
                  >
                    Ya, hapus semuanya
                  </button>
                ) : (
                  <button
                    type="button"
                    className={styles.btnGhost}
                    onClick={() => setAskReset(true)}
                  >
                    Hapus dan mulai dari awal
                  </button>
                )}
              </div>
            </div>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (name.trim().length >= 2) startNew();
              }}
            >
              <div className={styles.field}>
                <label className={styles.label} htmlFor="who">
                  Nama lengkapmu
                </label>
                <input
                  id="who"
                  className={styles.input}
                  value={name}
                  maxLength={80}
                  autoComplete="off"
                  autoFocus
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <button
                type="submit"
                className={styles.btn}
                disabled={name.trim().length < 2}
              >
                Mulai hari ini
              </button>
            </form>
          )}
        </div>
      </div>
    );

  if (!session) return null;
  const a = session.answers;
  const idx = session.idx;
  const m: Mission = content.missions[idx];
  const last = idx === content.missions.length - 1;
  const elapsed = Math.max(0, Math.round((now - session.startedAt) / 1000));
  const left = TARGET_SECONDS - elapsed;
  const seed = `${session.attemptId}${m.id}`;
  const props = { a, up: update, tele, seed };
  const untouched = content.missions
    .filter((x) => !answered(x.id, a))
    .map((x) => x.no);
  const w = m.widget;

  return (
    <div className={styles.page}>
      <header className={styles.bar}>
        <div className={styles.wrap}>
          <div className={styles.barRow}>
            <div className={styles.brand}>
              <span className={styles.brandName}>{content.title}</span>
              <span className={styles.clock}>
                <strong>{m.storyTime}</strong>
                waktu cerita
              </span>
            </div>
            <div className={styles.barTools}>
              <span className={styles.timer} aria-label="Waktu">
                {left >= 0 ? `Sisa ${mmss(left)}` : `Lewat ${mmss(-left)}`}
              </span>
              <button
                type="button"
                className={styles.btnGhost}
                aria-expanded={calc}
                onClick={() => setCalc((c) => !c)}
              >
                Kalkulator
              </button>
            </div>
          </div>
          <div
            className={styles.track}
            role="progressbar"
            aria-label="Jam cerita 08.30 sampai 17.00"
            aria-valuemin={0}
            aria-valuemax={510}
            aria-valuenow={minutesOf(m.storyTime)}
          >
            <div
              className={styles.trackFill}
              style={{ width: `${(minutesOf(m.storyTime) / 510) * 100}%` }}
            />
          </div>
        </div>
      </header>

      <nav className={`${styles.wrap} ${styles.chips}`} aria-label="Misi">
        {content.missions.map((x, i) => (
          <button
            key={x.id}
            type="button"
            className={`${styles.chip} ${answered(x.id, a) ? styles.chipDone : ""}`}
            aria-current={i === idx ? "step" : undefined}
            aria-label={`Misi ${x.no}, ${x.storyTime}`}
            onClick={() => go(i)}
          >
            {x.no}
          </button>
        ))}
      </nav>

      <main className={`${styles.wrap} ${styles.stage}`}>
        <section key={m.id} aria-labelledby="mission-title">
          <p className={styles.eyebrow}>
            Misi {m.no} dari {content.missions.length} · {m.storyTime}
          </p>
          <h1
            id="mission-title"
            ref={heading}
            tabIndex={-1}
            className={styles.missionTitle}
          >
            {m.title}
          </h1>
          <div className={styles.chat} aria-live="polite">
            {idx === 0 && (
              <Bubble msg={content.opening} content={content} i={0} />
            )}
            {m.messages.map((x, i) => (
              <Bubble
                key={i}
                msg={x}
                content={content}
                i={i + (idx === 0 ? 1 : 0)}
              />
            ))}
          </div>
          {w.kind === "sort" && <SortWidget w={w} {...props} />}
          {w.kind === "match" && <MatchWidget w={w} {...props} />}
          {w.kind === "hotspot" && <HotspotWidget w={w} {...props} />}
          {w.kind === "math" && <MathWidget w={w} {...props} />}
          {w.kind === "recon" && <ReconWidget w={w} {...props} />}
          {w.kind === "portal" && <PortalWidget w={w} {...props} />}
          {w.kind === "reply" && <ReplyWidget w={w} {...props} />}
          {w.kind === "integrity" && <IntegrityWidget w={w} {...props} />}
        </section>

        <div className={styles.nav}>
          <button
            type="button"
            className={styles.btnGhost}
            disabled={idx === 0}
            onClick={() => go(idx - 1)}
          >
            Kembali
          </button>
          <span className={styles.saved} role="status">
            {dirty ? "Tersimpan" : ""}
          </span>
          {last ? (
            <button
              type="button"
              className={styles.btn}
              onClick={() => setConfirmSend(true)}
              disabled={sending}
            >
              Selesai dan kirim jawaban
            </button>
          ) : (
            <button
              type="button"
              className={styles.btn}
              onClick={() => go(idx + 1)}
            >
              {w.kind === "reply" ? "Kirim balasan dan lanjut" : "Lanjut"}
            </button>
          )}
        </div>

        {last && confirmSend && (
          <div
            className={styles.confirm}
            role="alertdialog"
            aria-label="Konfirmasi pengiriman"
          >
            <p>
              <strong>Kirim jawabanmu sekarang?</strong> Setelah dikirim,
              jawaban tidak bisa diubah.
              {untouched.length > 0 &&
                ` Misi yang belum kamu isi: ${untouched.join(", ")}.`}
            </p>
            {sendError && (
              <p className={styles.error} role="alert">
                {sendError === "auth"
                  ? "Sesi PIN berakhir. Muat ulang halaman dan masukkan PIN lagi; jawabanmu tetap tersimpan di perangkat ini."
                  : "Pengiriman gagal. Jawabanmu masih aman di layar ini. Tekan Coba kirim lagi, atau hubungi pewawancara."}
              </p>
            )}
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.btn}
                onClick={send}
                disabled={sending}
              >
                {sending
                  ? "Mengirim..."
                  : sendError
                    ? "Coba kirim lagi"
                    : "Ya, kirim"}
              </button>
              <button
                type="button"
                className={styles.btnGhost}
                onClick={() => setConfirmSend(false)}
                disabled={sending}
              >
                Periksa lagi
              </button>
            </div>
          </div>
        )}
      </main>

      {calc && <Calculator onClose={() => setCalc(false)} />}
      {toast && (
        <div className={styles.toast} role="status">
          {toast}
        </div>
      )}
    </div>
  );
}

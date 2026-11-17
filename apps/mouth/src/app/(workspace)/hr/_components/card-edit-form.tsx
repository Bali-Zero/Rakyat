"use client";
/**
 * The HR card edit form (STAFF_CARD.md C11): edit, clear or create one person's card.
 *
 * Personal data never reaches a DOM ATTRIBUTE. Sentry's click and keypress breadcrumbs serialise
 * id, class and aria-label/title/name/alt/type, and an input's `value` attribute is what a DOM
 * snapshot carries, so every field is UNCONTROLLED: the current value is written to the DOM
 * PROPERTY (`el.value = x`) through a ref after mount and read back from the ref on submit.
 * No `value`, `defaultValue`, `placeholder`, `aria-label`, `title`, `name` or `id` prop is
 * ever given to a field. The form renders inside the page's protected region
 * (`.sentry-block`); it is inline, not portaled, so it needs no wrapper of its own.
 *
 * It only builds the request body (changed keys, plus `clear` for fields emptied) and shows
 * the answer's fixed messages; sending, and binding the answer to the session, is the page's.
 */
import React, { useEffect, useRef, useState } from "react";
import type {
  CardEditBody,
  CardEditFieldError,
  HrStaffCard,
} from "@/lib/api/hr/hr";
import { checkWhatsapp, maskWhatsapp } from "@/lib/hr/whatsapp";
import { CARD, HAIR, INK, SOFT, TONE } from "./hr-panels";

export type CardEditMode = "edit" | "create";
export type CardEditResult =
  { ok: true } | { ok: false; errors: CardEditFieldError[]; general?: string };

type Where = "card" | "profile" | "contract";
type Kind = "text" | "textarea" | "date" | "ptkp";
interface FieldDef {
  key: string;
  label: string;
  where: Where;
  kind: Kind;
  max: number;
  clearable: boolean;
  required?: boolean; // required in create mode, and never emptied in edit mode
  check?: (v: string) => string | null;
}

const PTKP_CODES = [
  "TK/0",
  "TK/1",
  "TK/2",
  "TK/3",
  "K/0",
  "K/1",
  "K/2",
  "K/3",
] as const;

const digits = (re: RegExp, msg: string) => (v: string) =>
  re.test(v.replace(/[ .-]/g, "")) ? null : msg;
const NIK = digits(/^[0-9]{16}$/, "Must be 16 digits");
const NPWP = digits(/^[0-9]{15,16}$/, "Must be 15 or 16 digits");
const BPJS_KES = digits(/^[0-9]{13}$/, "Must be 13 digits");
const BPJS_KET = digits(/^[A-Za-z0-9]{11}$/, "Must be 11 letters or digits");
const BANK = (v: string) =>
  /^[0-9]{6,20}$/.test(v.replace(/[ -]/g, ""))
    ? null
    : "Must be 6 to 20 digits";

const f = (
  key: string,
  label: string,
  where: Where,
  kind: Kind,
  max: number,
  extra: Partial<FieldDef> = {},
): FieldDef => ({ key, label, where, kind, max, clearable: true, ...extra });

export const FORM_GROUPS: { title: string; fields: FieldDef[] }[] = [
  {
    title: "Identity",
    fields: [
      f("legal_name", "Legal name", "profile", "text", 255),
      f("nik", "NIK", "profile", "text", 32, { check: NIK }),
      f("birth_place", "Birth place", "profile", "text", 100),
      f("birth_date", "Birth date", "profile", "date", 10),
      f("gender", "Gender", "profile", "text", 20),
      f("religion", "Religion", "profile", "text", 50),
      f("marital_status", "Marital status", "profile", "text", 50),
      f("address", "Address", "profile", "textarea", 500),
    ],
  },
  {
    title: "Employment",
    fields: [
      f("employer_entity", "Employer entity", "profile", "text", 255),
      f("unit", "Unit", "profile", "text", 100),
      f("position", "Position", "profile", "text", 100),
      f("hire_date", "Hire date", "card", "date", 10, {
        clearable: false,
        required: true,
      }),
      f("end_date", "End date", "profile", "date", 10),
      f("contract_number", "Contract number", "contract", "text", 100),
    ],
  },
  {
    title: "Tax & BPJS",
    fields: [
      f("npwp", "NPWP", "card", "text", 32, { check: NPWP }),
      f("ptkp_status", "PTKP status", "card", "ptkp", 10, {
        clearable: false,
      }),
      f("bpjs_kesehatan_number", "BPJS Kesehatan", "card", "text", 32, {
        check: BPJS_KES,
      }),
      f(
        "bpjs_ketenagakerjaan_number",
        "BPJS Ketenagakerjaan",
        "card",
        "text",
        32,
        {
          check: BPJS_KET,
        },
      ),
    ],
  },
  {
    title: "Bank",
    fields: [
      f("bank_name", "Bank name", "card", "text", 100),
      f("bank_account", "Account number", "card", "text", 100, {
        check: BANK,
      }),
      f("bank_account_holder", "Account holder", "card", "text", 255),
    ],
  },
];
const ALL_FIELDS = FORM_GROUPS.flatMap((g) => g.fields);

/** The stored value of a field as the form shows it ("" when empty). */
function stored(card: HrStaffCard | null, def: FieldDef): string {
  if (!card) return def.key === "ptkp_status" ? "TK/0" : "";
  const raw =
    def.key === "contract_number"
      ? card.contract?.number
      : (card as unknown as Record<string, unknown>)[def.key];
  return typeof raw === "string" ? raw : "";
}

type El = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;

const INPUT_CLS = `w-full rounded-xl border ${HAIR} bg-transparent px-3 py-2 text-[14px] outline-none focus:shadow-md ${INK}`;

export function CardEditForm({
  mode,
  card,
  whatsappMasked = null,
  onSubmit,
  onCancel,
}: {
  mode: CardEditMode;
  card: HrStaffCard | null;
  /** The stored WhatsApp number, masked by the server (DAI-153); the full one never comes here. */
  whatsappMasked?: string | null;
  onSubmit: (body: CardEditBody) => Promise<CardEditResult>;
  onCancel: () => void;
}) {
  const refs = useRef<Record<string, El | null>>({});
  // Starts empty and stays uncontrolled like every field: empty = keep the stored number.
  const waRef = useRef<HTMLInputElement | null>(null);
  const shownWa = maskWhatsapp(whatsappMasked);
  const activeRef = useRef<HTMLInputElement | null>(null);
  const payrollRef = useRef<HTMLInputElement | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [general, setGeneral] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const initial = mode === "create" ? null : card;

  // Current values go in as DOM properties after mount, never as attributes.
  useEffect(() => {
    for (const def of ALL_FIELDS) {
      const el = refs.current[def.key];
      if (el) el.value = stored(initial, def);
    }
    if (activeRef.current)
      activeRef.current.checked = initial?.is_active ?? true;
    if (payrollRef.current)
      payrollRef.current.checked = initial?.on_payroll !== false;
    // mounted once per person: the page keys the form by member and mode
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const read = (def: FieldDef): string => {
    const raw = refs.current[def.key]?.value ?? "";
    return def.kind === "textarea"
      ? raw.replace(/\r\n/g, "\n").trim()
      : raw.trim();
  };

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    const next: Record<string, string> = {};
    const cardPatch: Record<string, unknown> = {};
    const profile: Record<string, unknown> = {};
    const clear: string[] = [];
    for (const def of ALL_FIELDS) {
      const was = stored(initial, def);
      const now = read(def);
      if (now === was.trim() && !(def.required && mode === "create")) continue;
      if (now === "") {
        if (def.required) next[def.key] = "Required";
        else if (def.clearable && was.trim() !== "") clear.push(def.key);
        continue;
      }
      if (now.length > def.max) {
        next[def.key] = `At most ${def.max} characters`;
        continue;
      }
      const bad = def.check?.(now);
      if (bad) {
        next[def.key] = bad;
        continue;
      }
      if (def.where === "card") cardPatch[def.key] = now;
      else if (def.where === "contract") profile["contract"] = { number: now };
      else profile[def.key] = now;
    }
    const wasActive = initial?.is_active ?? true;
    const isActive = activeRef.current?.checked ?? wasActive;
    if (isActive !== wasActive) cardPatch["is_active"] = isActive;
    // On payroll: false is stored; turning it back on clears the key (absent means true).
    const wasOnPayroll = initial?.on_payroll !== false;
    const onPayroll = payrollRef.current?.checked ?? wasOnPayroll;
    if (onPayroll !== wasOnPayroll) {
      if (onPayroll) clear.push("on_payroll");
      else profile["on_payroll"] = false;
    }
    if (profile["contract"] === undefined) delete profile["contract"];
    if (Object.keys(profile).length) cardPatch["profile"] = profile;
    const wa = checkWhatsapp(waRef.current?.value ?? "");
    if (!wa.ok) next["whatsapp"] = wa.message;
    const whatsapp = wa.ok ? wa.value : null;

    setErrors(next);
    setGeneral(null);
    if (Object.keys(next).length) return;
    if (!Object.keys(cardPatch).length && !clear.length && whatsapp === null) {
      setGeneral("Nothing changed.");
      return;
    }
    const body: CardEditBody = { card: cardPatch as CardEditBody["card"] };
    if (clear.length) body.clear = clear as CardEditBody["clear"];
    if (whatsapp !== null) body.whatsapp = whatsapp;
    setSaving(true);
    const res = await onSubmit(body).catch((): CardEditResult => ({
      ok: false,
      errors: [],
      general: "Could not save. Try again.",
    }));
    setSaving(false);
    if (res.ok) return;
    const byField: Record<string, string> = {};
    let rest: string | null = res.general ?? null;
    for (const err of res.errors) {
      if (
        err.field &&
        (err.field === "whatsapp" ||
          ALL_FIELDS.some((d) => d.key === err.field))
      )
        byField[err.field] ??= err.message;
      else rest ??= err.message;
    }
    setErrors(byField);
    setGeneral(rest);
  }

  return (
    <form
      onSubmit={submit}
      noValidate
      className={`rounded-[28px] border p-6 md:p-8`}
      style={{
        borderColor: `color-mix(in srgb, ${TONE.me} 30%, ${CARD})`,
        background: CARD,
      }}
    >
      <h3 className={`text-[20px] font-semibold ${INK}`}>
        {mode === "create" ? "Add to HR" : "Edit HR card"}
      </h3>
      <p className={`mt-1 text-[13px] ${SOFT}`}>
        {mode === "create"
          ? "Fill what you know. The hire date is required."
          : "Empty a field to clear it. Only the fields you change are saved."}
      </p>
      {FORM_GROUPS.map((g) => (
        <fieldset key={g.title} className="mt-6">
          <legend
            className={`mb-3 text-[12px] font-bold uppercase tracking-wider ${SOFT}`}
          >
            {g.title}
          </legend>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {g.fields.map((def) => (
              <label
                key={def.key}
                className={`flex flex-col gap-1 ${def.kind === "textarea" ? "md:col-span-2" : ""}`}
              >
                <span className={`text-[13px] font-medium ${INK}`}>
                  {def.label}
                  {def.required && mode === "create" ? " *" : ""}
                </span>
                {def.kind === "textarea" ? (
                  <textarea
                    ref={(el) => {
                      refs.current[def.key] = el;
                    }}
                    rows={3}
                    maxLength={def.max}
                    className={INPUT_CLS}
                  />
                ) : def.kind === "ptkp" ? (
                  <select
                    ref={(el) => {
                      refs.current[def.key] = el;
                    }}
                    className={INPUT_CLS}
                  >
                    {PTKP_CODES.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    ref={(el) => {
                      refs.current[def.key] = el;
                    }}
                    type={def.kind === "date" ? "date" : "text"}
                    maxLength={def.kind === "date" ? undefined : def.max}
                    autoComplete="off"
                    spellCheck={false}
                    className={INPUT_CLS}
                  />
                )}
                {errors[def.key] && (
                  <span role="alert" className="text-[12px] text-red-600">
                    {errors[def.key]}
                  </span>
                )}
              </label>
            ))}
            {g.title === "Employment" && (
              <label className="flex items-center gap-2">
                <input ref={activeRef} type="checkbox" />
                <span className={`text-[13px] font-medium ${INK}`}>Active</span>
              </label>
            )}
            {g.title === "Employment" && (
              <label className="flex items-center gap-2">
                <input ref={payrollRef} type="checkbox" role="switch" />
                <span className={`text-[13px] font-medium ${INK}`}>
                  On payroll
                </span>
              </label>
            )}
          </div>
        </fieldset>
      ))}
      <fieldset className="mt-6">
        <legend
          className={`mb-3 text-[12px] font-bold uppercase tracking-wider ${SOFT}`}
        >
          Contact
        </legend>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <label className="flex flex-col gap-1">
            <span className={`text-[13px] font-medium ${INK}`}>WhatsApp</span>
            <span className={`text-[12px] ${SOFT}`} data-testid="hr-wa-current">
              {shownWa ? `On file: ${shownWa}` : "No number on file"}
            </span>
            <input
              ref={waRef}
              type="tel"
              inputMode="tel"
              maxLength={32}
              autoComplete="off"
              spellCheck={false}
              className={INPUT_CLS}
            />
            <span className={`text-[12px] ${SOFT}`}>
              New number, starting with 62. Leave empty to keep the current one.
            </span>
            {errors["whatsapp"] && (
              <span role="alert" className="text-[12px] text-red-600">
                {errors["whatsapp"]}
              </span>
            )}
          </label>
        </div>
      </fieldset>
      {general && (
        <p role="alert" className="mt-4 text-[13px] text-red-600">
          {general}
        </p>
      )}
      <div className="mt-6 flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={saving}
          className="rounded-full px-5 py-2 text-[13px] font-semibold text-white disabled:opacity-60"
          style={{ background: TONE.me }}
        >
          {saving ? "Saving" : mode === "create" ? "Add to HR" : "Save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className={`rounded-full border px-5 py-2 text-[13px] font-semibold ${HAIR} ${INK}`}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

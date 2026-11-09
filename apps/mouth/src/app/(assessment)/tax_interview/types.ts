/**
 * Client-safe shapes for /tax_interview. NOTHING in this file may carry an
 * answer key, a rubric or a point value: client components import it, so it
 * ships in the public bundle. Candidate-facing text lives in `_server/content`
 * and reaches the browser only as props, after the PIN cookie check.
 */
import type { ExerciseTelemetry } from "@/app/(assessment)/interview_natalie/telemetry";

export type Sender = "lestari" | "galih" | "nindy" | "naomi" | "hartley";

export interface Attachment {
  variant: "error" | "letter";
  title: string;
  lines: string[];
  foot?: string;
}

export interface ChatMsg {
  from: Sender;
  at: string;
  text: string;
  en?: boolean;
  attachment?: Attachment;
}

export interface Persona {
  name: string;
  role: string;
  initials: string;
}

export interface Opt {
  id: string;
  label: string;
}

export interface MatchPart {
  title: string;
  note: string;
  rows: Opt[];
  options: Opt[];
}

export interface DocField {
  id: string;
  label: string;
  value: string;
}

export interface ReconRow {
  id: string;
  date: string;
  memo: string;
  dk: "D" | "K";
  amount: string;
  booked: string;
}

export interface MathProblem {
  id: string;
  title: string;
  text: string;
  inputs: Opt[];
  choice?: { id: string; text: string; options: Opt[] };
}

export interface Situation {
  id: string;
  title: string;
  text: string;
  options: Opt[];
}

export type Widget =
  | { kind: "sort"; cards: Opt[] }
  | { kind: "match"; parts: MatchPart[] }
  | {
      kind: "hotspot";
      invoice: { title: string; lines: { label: string; value: string }[] };
      doc: { title: string; fields: DocField[] };
    }
  | { kind: "math"; problems: MathProblem[] }
  | {
      kind: "recon";
      invoices: string[][];
      rows: ReconRow[];
      statuses: Opt[];
      questions: { id: string; text: string; options: Opt[] }[];
    }
  | {
      kind: "portal";
      pick: string;
      options: Opt[];
      prompt: string;
      maxWords: number;
    }
  | { kind: "reply"; infoTitle: string; info: string; maxWords: number }
  | { kind: "integrity"; situations: Situation[]; maxWords: number };

export interface Mission {
  id: string;
  no: number;
  storyTime: string;
  minutes: number;
  title: string;
  messages: ChatMsg[];
  widget: Widget;
}

export interface Content {
  title: string;
  subtitle: string;
  personas: Record<Sender, Persona>;
  opening: ChatMsg;
  closing: ChatMsg;
  missions: Mission[];
}

export interface Answers {
  /** null until the candidate touches the cards: an unmoved list is "no answer". */
  m1: string[] | null;
  m2: Record<string, string>;
  m3: string[];
  m4: Record<string, string>;
  m5: Record<string, string>;
  m6: { checked: string[]; note: string };
  m7: { reply: string };
  m8: Record<string, { choice: string; reason: string }>;
}

export const emptyAnswers = (): Answers => ({
  m1: null,
  m2: {},
  m3: [],
  m4: {},
  m5: {},
  m6: { checked: [], note: "" },
  m7: { reply: "" },
  m8: {},
});

export interface SubmitPayload {
  attemptId: string;
  name: string;
  startedAt: number;
  endedAt: number;
  answers: Answers;
  telemetry: Record<string, ExerciseTelemetry>;
}

export const MISSION_IDS = [
  "m1_triage",
  "m2_calendar",
  "m3_hotspots",
  "m4_math",
  "m5_recon",
  "m6_portal_down",
  "m7_english_reply",
  "m8_integrity",
] as const;

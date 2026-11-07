import { getAllCodes, getCode } from "@/lib/kbli-data";

export interface KbliSuccessor {
  code: string;
  title: string;
}

export interface KbliResolution {
  /** true when /kbli/<code> exists in the navigator (KBLI 2025 set). */
  inNavigator: boolean;
  /** Title exactly as the navigator page body shows it (titleEn). */
  title?: string;
  /** KBLI 2025 successors, from the BPS 2020-2025 crosswalk, only for codes not in the navigator. */
  successors: KbliSuccessor[];
}

let successorIndex: Map<string, string[]> | null = null;

// Reverse index of the BPS crosswalk the navigator already ships:
// every 2025 record lists its KBLI 2020 ancestors (bps_2020_ancestors).
function successorsOf(code2020: string): string[] {
  if (successorIndex === null) {
    successorIndex = new Map();
    for (const rec of getAllCodes()) {
      for (const ancestor of rec.transition.bpsCrosswalk?.codes ?? []) {
        const list = successorIndex.get(ancestor) ?? [];
        list.push(rec.code);
        successorIndex.set(ancestor, list);
      }
    }
  }
  return successorIndex.get(code2020) ?? [];
}

export function resolveKbliCodes(
  codes: readonly string[],
): Record<string, KbliResolution> {
  const out: Record<string, KbliResolution> = {};
  for (const code of codes) {
    const rec = getCode(code);
    if (rec) {
      out[code] = { inNavigator: true, title: rec.titleEn, successors: [] };
      continue;
    }
    const successors: KbliSuccessor[] = [];
    for (const next of successorsOf(code)) {
      const nextRec = getCode(next);
      if (nextRec) successors.push({ code: next, title: nextRec.titleEn });
    }
    out[code] = { inNavigator: false, successors };
  }
  return out;
}

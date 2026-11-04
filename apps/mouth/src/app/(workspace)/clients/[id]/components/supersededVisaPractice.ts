import type { ClientProfile } from "@/lib/api/crm/crm.types";

type Practice = ClientProfile["practices"][number];

const VISA_MARKERS = ["visa", "kitas", "kitap"];

const isVisaPractice = (p: Practice): boolean =>
  VISA_MARKERS.some(
    (m) =>
      p.practice_type_code?.toLowerCase().includes(m) ||
      p.practice_type_name?.toLowerCase().includes(m),
  );

// A completed visa/KITAS process that a later visa/KITAS process for the same
// person (same family_member_id, both empty = the client) replaced. Its expiry
// chip should not ask for attention any more: client 10247 (6 Oct) showed the
// old tourist visa (practice 41) in warning tone after KITAS practice 311 was done. "Later" =
// higher practice id (ids grow with creation); a cancelled process replaces
// nothing; a process still running is never marked.
export function supersededVisaPracticeIds(practices: Practice[]): Set<number> {
  const visas = practices.filter(
    (p) => isVisaPractice(p) && p.status !== "cancelled",
  );
  const out = new Set<number>();
  for (const p of visas) {
    if (p.status !== "completed") continue;
    const person = p.family_member_id ?? null;
    if (
      visas.some((q) => q.id > p.id && (q.family_member_id ?? null) === person)
    ) {
      out.add(p.id);
    }
  }
  return out;
}

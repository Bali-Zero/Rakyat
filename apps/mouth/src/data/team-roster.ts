// apps/mouth/src/data/team-roster.ts
// ─────────────────────────────────────────────────────────────────────────────
// SINGLE SOURCE OF TRUTH for the Bali Zero team roster (photos, roles, depts).
//
// WHY THIS FILE EXISTS:
//   Team photos + roles used to be hardcoded in FOUR independent frontend lists
//   (book-data.ts, (blog)/team/page.tsx, v2/company/about, v2/_components/SocialProof)
//   PLUS the Postgres team_members.avatar column — all diverging (different members,
//   different roles, Heru/Zainal photos swapped in 2 files, 11 members on batik
//   placeholders). Updating "all the team photos" meant editing 5 places by hand.
//
//   This is now the ONE place. Every frontend list derives from TEAM_ROSTER. The DB
//   team_members.avatar column is aligned to the same /static/team/<slug>.<ext> paths
//   (see migration / scripts/sync_team_avatars.sql) so site + CRM portal stay coherent.
//
// HOW TO UPDATE A PHOTO:
//   1. Drop the file in apps/mouth/public/static/team/<slug>.<ext> (compress to ~<100KB,
//      ≤800px long edge — see scripts/process_team_photos.sh).
//   2. Set `photo` below. Done — all public surfaces pick it up.
//   3. For the CRM portal avatar, also run the avatar-sync (DB) step.
//
// CONVENTIONS:
//   - `slug`     : stable key = email prefix (used for photo filename + DB join).
//   - `photo`    : "/static/team/<slug>.<ext>" or undefined → UI falls back to initials.
//   - `email`    : canonical @balizero.com address (matches team_members.email; some
//                  prefixes differ from slug — see each entry's `email`).
//   - `dept`     : coarse public grouping. `liveDept` mirrors the DB department when it
//                  differs (kept for reference; public pages use `dept`).
//   - `publicListed` : false = real teammate but NOT shown on public marketing pages.
// ─────────────────────────────────────────────────────────────────────────────

export type TeamDept =
  | "leadership"
  | "setup"
  | "tax"
  | "accounting"
  | "support"
  | "immigration"
  | "operations"
  | "security";

export interface RosterMember {
  slug: string;
  name: string;
  role: string; // public-facing role label
  dept: TeamDept; // public grouping
  email: string;
  photo?: string; // /static/team/<slug>.<ext>; undefined → initials fallback
  publicListed?: boolean; // default true; false hides from public marketing pages
}

// Helper: initials from a display name (max 2).
// Re-exported, not defined here: the helper itself lives in
// `src/lib/team-initials.ts` so a "use client" component can use it WITHOUT
// pulling this file — and therefore the whole roster — into a public JS chunk.
// This re-export keeps the module's existing API for server-side callers.
export { initialsOf } from "@/lib/team-initials";

// ── THE ROSTER ───────────────────────────────────────────────────────────────
// Verified 2026-06-15 against team_members DB + founder confirmation.
// Photos delivered by Antonello 2026-06-15; batik placeholders removed where a real
// photo now exists. Members still without a real photo render the initials fallback.
export const TEAM_ROSTER: RosterMember[] = [
  // ── Leadership ──────────────────────────────────────────────────────────
  {
    slug: "zainal",
    name: "Zainal Abidin",
    role: "CEO",
    dept: "leadership",
    email: "zainal@balizero.com",
    photo: "/static/team/zainal-a1.jpg",
  },
  {
    slug: "heru",
    name: "Pak Heru",
    role: "Commissioner",
    dept: "leadership",
    email: "heru@balizero.com",
    photo: "/static/team/heru-a1.jpg",
  },
  {
    slug: "ruslana",
    name: "Ruslana",
    role: "Board Member",
    dept: "leadership",
    email: "ruslana@balizero.com",
    photo: "/static/team/ruslana-2026-11.jpg",
  },
  {
    slug: "dian",
    name: "Dian Purnomo",
    role: "Corporate Manager",
    dept: "leadership",
    email: "dian.corporate@balizero.com",
    photo: "/static/team/dian.jpg",
  },
  {
    slug: "adi",
    name: "Adi",
    role: "Operations Manager",
    dept: "leadership",
    email: "adi.manager@balizero.com",
    photo: "/static/team/adi.jpg",
  },

  // ── Setup ───────────────────────────────────────────────────────────────
  {
    slug: "adit",
    name: "Adit",
    role: "Director",
    dept: "setup",
    email: "adit@balizero.com",
    photo: "/static/team/adit.jpg",
  },
  {
    slug: "ari",
    name: "Ari",
    role: "Tax Responsible",
    dept: "setup",
    email: "ari.firda@balizero.com",
    photo: "/static/team/ari.jpg",
  },
  {
    slug: "krisna",
    name: "Krisna",
    role: "Senior Consultant",
    dept: "setup",
    email: "krisna@balizero.com",
    photo: "/static/team/krisna-20260927.jpg",
  },
  {
    slug: "surya",
    name: "Surya",
    role: "Senior Consultant",
    dept: "setup",
    email: "surya@balizero.com",
    photo: "/static/team/surya.jpg",
  },
  {
    slug: "damar",
    name: "Damar",
    role: "Junior Consultant",
    dept: "setup",
    email: "damar@balizero.com",
    photo: "/static/team/damar.jpg",
  },
  {
    slug: "vino",
    name: "Vino",
    role: "Junior Consultant",
    dept: "setup",
    email: "vino@balizero.com",
    photo: "/static/team/vino.jpg",
  },
  {
    slug: "rio",
    name: "Rio",
    role: "Junior Consultant",
    dept: "setup",
    email: "rio@balizero.com",
    photo: "/static/team/rio.jpg",
  },

  // ── Tax ─────────────────────────────────────────────────────────────────
  {
    slug: "angel",
    name: "Angel",
    role: "Tax Supervisor",
    dept: "tax",
    email: "angel.tax@balizero.com",
    photo: "/static/team/angel.jpg",
  },
  {
    slug: "kadek",
    name: "Kadek",
    role: "Tax Consultant",
    dept: "tax",
    email: "kadek.tax@balizero.com",
    photo: "/static/team/kadek.jpg",
  },
  {
    slug: "dewaayu",
    name: "Dewa Ayu",
    role: "Tax Consultant",
    dept: "tax",
    email: "dewaayu.tax@balizero.com",
    photo: "/static/team/dewaayu.jpg",
  },
  {
    slug: "dea",
    name: "Dea",
    role: "Tax Consultant",
    dept: "tax",
    email: "dea@balizero.com",
    photo: "/static/team/dea.jpg",
  },
  {
    slug: "candra",
    name: "Candra",
    role: "Tax Consultant",
    dept: "tax",
    email: "candra@balizero.com",
    photo: "/static/team/candra.jpg",
  },
  {
    slug: "faisha",
    name: "Faisha",
    role: "Tax Care",
    dept: "tax",
    email: "faysha.tax@balizero.com",
    // Portrait withdrawn from public/ under D6 (2026-09-15, owner ruling: default
    // privacy for the two people excluded from public marketing surfaces — see
    // PUBLIC_EXCLUDED_SLUGS in src/lib/team-public-listing.ts). No `photo` → this
    // member renders the initials fallback per the `photo?` contract above.
  },

  // ── Accounting ──────────────────────────────────────────────────────────
  {
    slug: "asya",
    name: "Asya Nadia",
    role: "HR & Accounting",
    dept: "accounting",
    email: "asya@balizero.com",
    photo: "/static/team/asya.jpg",
  },
  {
    slug: "oman",
    name: "Oman Maturi",
    role: "Bank Relations",
    dept: "accounting",
    email: "oman@balizero.com",
    photo: "/static/team/oman.jpg",
  },
  {
    slug: "dini",
    name: "Herdini Agustin",
    role: "Finance & HR",
    dept: "accounting",
    email: "dini@balizero.com",
    photo: "/static/team/dini.jpg",
  },

  // ── Immigration ─────────────────────────────────────────────────────────
  {
    slug: "agus",
    name: "Agus Satrio",
    role: "Chief Immigration Denpasar",
    dept: "immigration",
    email: "agus@balizero.com",
    photo: "/static/team/agus.jpg",
  },
  {
    slug: "farid",
    name: "Farid Catur Prasetyo",
    role: "Chief Immigration Ngurah Rai",
    dept: "immigration",
    email: "farid@balizero.com",
    photo: "/static/team/farid.jpg",
  },
  {
    slug: "nanang",
    name: "Nanang Tri Jaya",
    role: "Immigration Denpasar",
    dept: "immigration",
    email: "nanang@balizero.com",
    photo: "/static/team/nanang.jpg",
  },

  // ── Operations ──────────────────────────────────────────────────────────
  {
    slug: "yoyok",
    name: "Yoyok Supriadi",
    role: "General Affairs",
    dept: "operations",
    email: "yoyok@balizero.com",
    photo: "/static/team/yoyok.jpg",
  },
  {
    slug: "yudi",
    name: "Yudi Agus Setiawan",
    role: "General Affairs",
    dept: "operations",
    email: "yudi@balizero.com",
    photo: "/static/team/yudi.jpg",
  },
  {
    slug: "megi",
    name: "Megi Saputra",
    role: "Business Licensing & OSS",
    dept: "operations",
    email: "megi@balizero.com",
    photo: "/static/team/megi.jpg",
  },
  {
    slug: "david",
    name: "David Prawira",
    role: "Immigration Processing",
    dept: "operations",
    email: "david@balizero.com",
    photo: "/static/team/david.jpg",
  },
  {
    slug: "novi",
    name: "Novitasari",
    role: "Immigration Processing Supervisor",
    dept: "operations",
    email: "novi@balizero.com",
    photo: "/static/team/novi.jpg",
  },
  {
    slug: "lia",
    name: "Lia Amaliya",
    role: "Immigration Processing",
    dept: "operations",
    email: "lia@balizero.com",
    photo: "/static/team/lia.jpg",
  },
  {
    slug: "yanti",
    name: "Sri Wijayanti",
    role: "Work Permit Supervisor",
    dept: "operations",
    email: "yanti@balizero.com",
    photo: "/static/team/yanti.jpg",
  },

  // ── Security ────────────────────────────────────────────────────────────
  {
    slug: "ropik",
    name: "Ropik",
    role: "Security",
    dept: "security",
    email: "ropik@balizero.com",
    photo: "/static/team/ropik.jpg",
  },

  // ── Support ─────────────────────────────────────────────────────────────
  {
    slug: "rina",
    name: "Rina",
    role: "Reception",
    dept: "support",
    email: "rina@balizero.com",
    photo: "/static/team/rina.jpg",
  },
  {
    slug: "sahira",
    name: "Sahira",
    role: "Marketing Specialist",
    dept: "support",
    email: "sahira@balizero.com",
    // Portrait withdrawn from public/ under D6 (2026-09-15, owner ruling: default
    // privacy for the two people excluded from public marketing surfaces — see
    // PUBLIC_EXCLUDED_SLUGS in src/lib/team-public-listing.ts). No `photo` → this
    // member renders the initials fallback per the `photo?` contract above.
  },
  {
    slug: "subhi",
    name: "Subhi",
    role: "AI",
    dept: "support",
    email: "subhi@balizero.com",
    photo: "/static/team/subhi.jpg",
  },
];

// ── Derived helpers (consumed by the page components) ─────────────────────────
export const PUBLIC_ROSTER: RosterMember[] = TEAM_ROSTER.filter(
  (m) => m.publicListed !== false,
);

export function rosterByDept(dept: TeamDept): RosterMember[] {
  return PUBLIC_ROSTER.filter((m) => m.dept === dept);
}

export function rosterBySlug(slug: string): RosterMember | undefined {
  return TEAM_ROSTER.find((m) => m.slug === slug);
}

/** Photo path for an email (matches the DB team_members.email join). */
export function photoForEmail(email: string): string | undefined {
  const prefix = email.split("@")[0].toLowerCase();
  const m =
    TEAM_ROSTER.find((r) => r.email.toLowerCase() === email.toLowerCase()) ??
    TEAM_ROSTER.find((r) => r.slug === prefix);
  return m?.photo;
}

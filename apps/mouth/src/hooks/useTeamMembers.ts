"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

interface TeamMember {
  id: string | null;
  email: string;
  full_name: string;
  name: string;
  role: string;
  avatar_url: string | null;
  avatar: string | null;
}

// Mirrors backend/app/utils/service_accounts.py NON_HUMAN_ROLES — a role that
// is not a client is not automatically a colleague. "monitoring" is the
// login-healthcheck probe: it authenticates like a team member every 5
// minutes and must never appear in a human-facing roster or assignment list.
// Keep in sync with the Python SSOT if a new service role is ever added.
const NON_HUMAN_ROLES = new Set(["client", "monitoring"]);

export function isNonHumanRole(role: string | null | undefined): boolean {
  return NON_HUMAN_ROLES.has((role ?? "").trim().toLowerCase());
}

// Mirrors backend/app/utils/service_accounts.py TEAM_ROLES — the ALLOW-list the
// team gate (require_team_member) admits, normalised. A role absent here is not
// a colleague, whatever else it is. Keep in sync with the Python SSOT; the
// backend tripwire test_service_accounts_ts_sync.py compares both sets.
const TEAM_ROLES = new Set([
  "admin",
  "team",
  "founder",
  "ceo",
  "board member",
  "team leader",
  "supervisor",
  "tax lead",
  "tax manager",
  "tax care",
  "accounting",
  "marketing & accounting",
  "marketing advisory",
  "executive consultant",
  "specialist advisor",
  "junior consultant",
  "consultant",
  "reception",
  "member",
  // roster decisions of 2026-10-20: Manager (two hires + setup) and
  // Finance & Client Services Coordinator
  "manager",
  "finance & client services coordinator",
  // reorganisation 2026-11-09: new job titles, same people, same access
  "direktur",
  "tax supervisor",
  "tax responsible",
  "senior consultant",
  "hr & accounting",
  // owner order 2026-11-15: Tax Consultant, same access as Tax Lead
  "tax consultant",
]);

export function isTeamRole(role: string | null | undefined): boolean {
  return TEAM_ROLES.has((role ?? "").trim().toLowerCase());
}

async function fetchTeamMembers(): Promise<TeamMember[]> {
  const res = await api.get<TeamMember[] | { members: TeamMember[] }>(
    "/api/team/members",
  );
  // Backend returns array directly; handle both formats for safety
  return Array.isArray(res) ? res : res.members;
}

// The roster changes a few times a month; every Kita page that names people
// reads it. Kept fresh for 30 minutes, in memory and — so a full page load
// (a new tab, a hard refresh, a link from email) does not pay the ~1.3 s
// request again — in this tab's sessionStorage, tagged with the viewer's
// email so a different login in the same tab never reads it.
export const TEAM_MEMBERS_STALE_MS = 30 * 60 * 1000;
const TEAM_MEMBERS_STORAGE_KEY = "kita:team-members:v1";

type StoredRoster = { viewer: string; at: number; members: TeamMember[] };

function currentViewer(): string {
  try {
    return api.getUserProfile?.()?.email?.trim().toLowerCase() ?? "";
  } catch {
    return "";
  }
}

export function readStoredTeamMembers(
  now = Date.now(),
): StoredRoster | undefined {
  try {
    const viewer = currentViewer();
    if (!viewer) return undefined;
    const raw = window.sessionStorage.getItem(TEAM_MEMBERS_STORAGE_KEY);
    if (!raw) return undefined;
    const stored = JSON.parse(raw) as StoredRoster;
    if (
      stored?.viewer !== viewer ||
      !Array.isArray(stored.members) ||
      typeof stored.at !== "number" ||
      now - stored.at >= TEAM_MEMBERS_STALE_MS
    ) {
      return undefined;
    }
    return stored;
  } catch {
    return undefined;
  }
}

function storeTeamMembers(members: TeamMember[]): void {
  try {
    const viewer = currentViewer();
    if (!viewer) return;
    const stored: StoredRoster = { viewer, at: Date.now(), members };
    window.sessionStorage.setItem(
      TEAM_MEMBERS_STORAGE_KEY,
      JSON.stringify(stored),
    );
  } catch {
    /* storage unavailable or full: the in-memory cache still applies */
  }
}

async function fetchAndStoreTeamMembers(): Promise<TeamMember[]> {
  const members = await fetchTeamMembers();
  storeTeamMembers(members);
  return members;
}

export function useTeamMembers() {
  return useQuery({
    queryKey: ["team-members"],
    queryFn: fetchAndStoreTeamMembers,
    initialData: () => readStoredTeamMembers()?.members,
    initialDataUpdatedAt: () => readStoredTeamMembers()?.at,
    staleTime: TEAM_MEMBERS_STALE_MS,
    gcTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
}

export function useTeamMemberOptions() {
  const { data: members = [], ...rest } = useTeamMembers();
  // Stabilize reference — without useMemo, filter+map creates a new array every render,
  // causing infinite re-render loops in consumers that depend on this in useEffect deps
  const options = useMemo(() => {
    const uniqueMembers = new Map<
      string,
      TeamMember & { normalizedEmail: string; displayName: string }
    >();

    for (const member of members) {
      const normalizedEmail = member.email.trim().toLowerCase();
      if (
        !normalizedEmail ||
        isNonHumanRole(member.role) ||
        uniqueMembers.has(normalizedEmail)
      ) {
        continue;
      }
      uniqueMembers.set(normalizedEmail, {
        ...member,
        normalizedEmail,
        displayName:
          member.full_name?.trim() || member.name?.trim() || normalizedEmail,
      });
    }

    const labelCounts = new Map<string, number>();
    for (const member of uniqueMembers.values()) {
      const key = member.displayName.toLowerCase();
      labelCounts.set(key, (labelCounts.get(key) ?? 0) + 1);
    }

    return Array.from(uniqueMembers.values()).map((member) => ({
      value: member.normalizedEmail,
      label:
        (labelCounts.get(member.displayName.toLowerCase()) ?? 0) > 1
          ? `${member.displayName} (${member.normalizedEmail})`
          : member.displayName,
      avatar: member.avatar_url ?? member.avatar ?? undefined,
    }));
  }, [members]);
  return { options, ...rest };
}

/**
 * Shareholders & officers — grouping and the link to a person's client card.
 *
 * The backend returns one row per (person, role); the tab shows one row per
 * person. `client_id` rides along so a person who is also a client in the CRM
 * can be a link to their card (the old grouping keyed on the display name only
 * and dropped it).
 */

export interface AssociateInput {
  client_id?: number | null;
  client_name?: string;
  role?: string;
  ownership_percentage?: number;
  shares_count?: number;
}

export interface Associate {
  client_id?: number;
  client_name?: string;
  role: string;
  ownership_percentage?: number;
  shares_count?: number;
}

export function groupAssociates(rows: AssociateInput[]): Associate[] {
  const grouped = new Map<
    string,
    {
      client_id?: number;
      client_name?: string;
      roles: string[];
      ownership_percentage?: number;
      shares_count?: number;
    }
  >();
  for (const a of rows) {
    // Same person = same client id; rows without one fall back to the name.
    const key = a.client_id
      ? `id:${a.client_id}`
      : `name:${a.client_name || "unknown"}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        client_id: a.client_id ?? undefined,
        client_name: a.client_name,
        roles: [],
        ownership_percentage: a.ownership_percentage,
        shares_count: a.shares_count,
      });
    }
    const entry = grouped.get(key)!;
    if (a.role && !entry.roles.includes(a.role)) entry.roles.push(a.role);
    if (
      a.shares_count &&
      (!entry.shares_count || a.shares_count > entry.shares_count)
    )
      entry.shares_count = a.shares_count;
    if (
      a.ownership_percentage &&
      (!entry.ownership_percentage ||
        a.ownership_percentage > entry.ownership_percentage)
    )
      entry.ownership_percentage = a.ownership_percentage;
  }
  return Array.from(grouped.values()).map((g) => ({
    client_id: g.client_id,
    client_name: g.client_name,
    role: g.roles.join(" / "),
    ownership_percentage: g.ownership_percentage,
    shares_count: g.shares_count,
  }));
}

const norm = (s?: string | null): string => (s ?? "").trim().toLowerCase();

/**
 * The client card a person row links to, or `null` for plain text.
 *
 * - A person carrying a `client_id` links to it.
 * - An OCR row (a name only) links when its name equals an associate's name,
 *   case-insensitively and trimmed, and that associate has a `client_id`.
 * - The client whose card this is never links to itself.
 */
export function personClientId(
  person: { client_id?: number; name?: string },
  associates: Associate[],
  currentClientId: number,
): number | null {
  let id: number | undefined = person.client_id;
  if (!id && norm(person.name)) {
    id = associates.find(
      (a) => a.client_id && norm(a.client_name) === norm(person.name),
    )?.client_id;
  }
  return id && id !== currentClientId ? id : null;
}

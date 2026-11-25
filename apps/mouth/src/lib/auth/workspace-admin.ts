export const WORKSPACE_ADMIN_ROLES: ReadonlySet<string> = new Set([
  "admin",
  "board member",
  "ceo",
  "founder",
  "owner",
  "board",
]);

export function isWorkspaceAdminRole(role: string | null | undefined): boolean {
  return !!role?.trim() && WORKSPACE_ADMIN_ROLES.has(role.trim().toLowerCase());
}

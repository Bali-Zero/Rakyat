import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { isWorkspaceAdminRole, WORKSPACE_ADMIN_ROLES } from "./workspace-admin";

describe("isWorkspaceAdminRole", () => {
  it("accepts CEO and Board Member roles with whitespace and casing normalized", () => {
    expect(isWorkspaceAdminRole("CEO")).toBe(true);
    expect(isWorkspaceAdminRole("Board Member")).toBe(true);
    expect(isWorkspaceAdminRole(" ceo ")).toBe(true);
    expect(isWorkspaceAdminRole("BOARD MEMBER")).toBe(true);
  });

  it("keeps existing frontend admin roles while rejecting unrelated roles", () => {
    for (const role of ["admin", "founder", "owner", "board"]) {
      expect(isWorkspaceAdminRole(role)).toBe(true);
    }

    for (const role of [
      "user",
      "staff",
      "consultant",
      "",
      null,
      undefined,
      "board-member",
      "ceo office",
    ]) {
      expect(isWorkspaceAdminRole(role)).toBe(false);
    }
  });

  it("contains every role accepted by the backend CRM admin check", () => {
    const backendPath = resolve(
      __dirname,
      "../../../../backend-rag/backend/app/utils/crm_utils.py",
    );
    const backendSource = readFileSync(backendPath, "utf8");
    const roleTuple = backendSource.match(
      /def is_crm_admin\(user: dict\).*?return role in \(([^)]*)\)/s,
    );

    expect(roleTuple, "Could not find is_crm_admin role tuple").not.toBeNull();
    const backendRoles = Array.from(
      roleTuple![1].matchAll(/"([^"]+)"/g),
      ([, role]) => role,
    );

    expect(backendRoles).not.toHaveLength(0);
    for (const role of backendRoles) {
      expect(WORKSPACE_ADMIN_ROLES.has(role)).toBe(true);
    }
  });
});

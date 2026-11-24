import { describe, it, expect } from "vitest";
import { WRITABLE_CLIENT_STATUSES } from "../crm.types";
import { clientStatusEnum, createClientSchema } from "../crm.schemas";

// Mirrors STATUS_VALUES in apps/backend-rag/backend/app/routers/crm_clients.py,
// which answers 422 to any other client status on create, update and filter.
const BACKEND_CLIENT_STATUSES = ["active", "inactive", "prospect", "lead"];

describe("client status lists", () => {
  it("GUILT: the statuses a user may write are only ones the backend accepts", () => {
    for (const { value } of WRITABLE_CLIENT_STATUSES) {
      expect(BACKEND_CLIENT_STATUSES).toContain(value);
    }
    const values = WRITABLE_CLIENT_STATUSES.map((s) => s.value) as string[];
    expect(values).not.toContain("completed");
    expect(values).not.toContain("lost");
  });

  it("the write schema accepts exactly the statuses the controls offer", () => {
    expect([...clientStatusEnum.options].sort()).toEqual(
      WRITABLE_CLIENT_STATUSES.map((s) => s.value).sort(),
    );
  });

  it("GUILT: a client created as prospect passes the write schema; completed and lost do not", () => {
    const base = { full_name: "Person One" };
    expect(
      createClientSchema.safeParse({ ...base, status: "prospect" }).success,
    ).toBe(true);
    expect(
      createClientSchema.safeParse({ ...base, status: "completed" }).success,
    ).toBe(false);
    expect(
      createClientSchema.safeParse({ ...base, status: "lost" }).success,
    ).toBe(false);
  });
});

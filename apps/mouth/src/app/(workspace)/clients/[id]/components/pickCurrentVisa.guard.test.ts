import { describe, expect, it } from "vitest";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { pickCurrentPermitFor, pickCurrentVisa } from "./pickCurrentVisa";

const doc = (
  over: Partial<ClientDocument> & { id: number },
): ClientDocument => ({
  client_id: 1,
  document_type: "kitas",
  document_category: "immigration",
  ...over,
});

describe("pickCurrentVisa — the Overview visa card shows the current permit", () => {
  it("control: among dated documents the farthest expiry wins, even if uploaded earlier", () => {
    const docs = [
      doc({
        id: 1,
        expiry_date: "2028-04-24",
        created_at: "2026-01-10T00:00:00Z",
      }),
      doc({
        id: 2,
        expiry_date: "2027-02-04",
        created_at: "2026-05-01T00:00:00Z",
      }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(1);
  });

  it("control: an undated document uploaded BEFORE the dated one does not win", () => {
    const docs = [
      doc({ id: 1, created_at: "2025-01-01T00:00:00Z" }),
      doc({
        id: 2,
        expiry_date: "2027-02-04",
        created_at: "2026-01-10T00:00:00Z",
      }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(2);
  });

  it("control: no visa documents -> undefined", () => {
    expect(
      pickCurrentVisa([
        doc({
          id: 1,
          document_type: "passport",
          document_category: "personal",
        }),
      ]),
    ).toBeUndefined();
  });

  it("GUILT (10247 shape): a new KITAS without expiry uploaded after the old visa is the current one", () => {
    const docs = [
      doc({
        id: 41,
        document_type: "visa",
        expiry_date: "2027-02-04",
        created_at: "2026-02-01T00:00:00Z",
      }),
      doc({ id: 7349, created_at: "2026-10-01T00:00:00Z" }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(7349);
  });

  it("GUILT: a visa the client removed in the portal (deleted_at) is not shown", () => {
    const docs = [
      doc({
        id: 1,
        expiry_date: "2029-01-01",
        created_at: "2026-01-01T00:00:00Z",
        deleted_at: "2026-09-01T00:00:00Z",
      }),
      doc({
        id: 2,
        expiry_date: "2027-02-04",
        created_at: "2026-01-01T00:00:00Z",
      }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(2);
  });

  it("GUILT: an archived visa is not shown", () => {
    const docs = [
      doc({ id: 1, expiry_date: "2029-01-01", is_archived: true }),
      doc({ id: 2, expiry_date: "2027-02-04" }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(2);
  });

  it("GUILT: a family member's KITAS is not the client's card", () => {
    const docs = [
      doc({ id: 1, expiry_date: "2029-01-01", family_member_id: 9 }),
      doc({ id: 2, expiry_date: "2027-02-04" }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(2);
  });
});

describe("pickCurrentVisa — the shared rule also serves the Immigration tab", () => {
  it("a VOA and a category-less visa row are candidates; a MERP never is", () => {
    expect(
      pickCurrentVisa([
        doc({ id: 1, document_type: "e-voa", expiry_date: "2027-01-01" }),
      ])?.id,
    ).toBe(1);
    expect(
      pickCurrentVisa([
        doc({ id: 2, document_category: undefined, expiry_date: "2027-01-01" }),
      ])?.id,
    ).toBe(2);
    expect(
      pickCurrentVisa([
        doc({
          id: 3,
          document_type: "visa",
          permit_family: "merp",
          expiry_date: "2030-01-01",
        }),
        doc({ id: 4, expiry_date: "2027-01-01" }),
      ])?.id,
    ).toBe(4);
  });

  it("pickCurrentPermitFor picks per holder", () => {
    const docs = [
      doc({ id: 1, expiry_date: "2030-01-01" }),
      doc({ id: 2, family_member_id: 9, expiry_date: "2026-01-01" }),
      doc({ id: 3, family_member_id: 9, expiry_date: "2028-01-01" }),
    ];
    expect(pickCurrentPermitFor(docs, 9)?.id).toBe(3);
    expect(pickCurrentPermitFor(docs, null)?.id).toBe(1);
  });
});

describe("pickCurrentVisa — a stay permit beats its own e-Visa copy", () => {
  const kitas = doc({
    id: 1,
    permit_code: "E28A",
    expiry_date: "2036-06-25",
    created_at: "2025-06-01T00:00:00Z",
  });
  const evisa = doc({
    id: 2,
    document_type: "evisa",
    permit_code: "E28A",
    expiry_date: "2036-06-25",
    created_at: "2025-06-10T00:00:00Z",
  });
  it("both orders pick the KITAS document", () => {
    expect(pickCurrentVisa([kitas, evisa])?.id).toBe(1);
    expect(pickCurrentVisa([evisa, kitas])?.id).toBe(1);
  });
  it("among several stay documents of the permit the latest issue_date wins", () => {
    const newer = doc({
      id: 3,
      permit_code: "E28A",
      expiry_date: "2036-06-25",
      issue_date: "2025-07-01",
    });
    expect(pickCurrentVisa([kitas, evisa, newer])?.id).toBe(3);
  });
  it("INNOCENCE: an e-Visa of a DIFFERENT permit (other expiry/code) is not displaced; a lone e-Visa is still picked", () => {
    const other = doc({
      id: 4,
      document_type: "evisa",
      permit_code: "C1",
      expiry_date: "2040-01-01",
    });
    expect(pickCurrentVisa([kitas, other])?.id).toBe(4);
    expect(pickCurrentVisa([evisa])?.id).toBe(2);
  });
  it("an ITK is a stay permit candidate", () => {
    expect(
      pickCurrentVisa([
        doc({ id: 5, document_type: "itk", expiry_date: "2027-01-01" }),
      ])?.id,
    ).toBe(5);
  });
});

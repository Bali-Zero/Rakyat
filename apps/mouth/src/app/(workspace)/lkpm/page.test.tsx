// The LKPM page must hand its client the LKPM assignee list, not the client
// tax-consultant options: lkpm_assigned_to has its own DB CHECK (widened to
// Dea and Candra by migration 364, owner decision 2026-11-18).
import React from "react";
import { describe, expect, it } from "vitest";

describe("LKPMBatchPage (server boundary)", () => {
  it("passes lkpmAssignees(), which includes Dea and Candra", async () => {
    const { default: LKPMBatchPage } = await import("./page");
    const { lkpmAssignees } = await import("@/lib/workspace/roster-directory");
    const props = (
      LKPMBatchPage() as React.ReactElement<{
        taxConsultants: { value: string }[];
      }>
    ).props;
    expect(props.taxConsultants).toEqual(lkpmAssignees());
    const values = props.taxConsultants.map((c) => c.value);
    expect(values).toContain("dea@balizero.com");
    expect(values).toContain("candra@balizero.com");
  });
});

// The LKPM page must hand its client the LKPM assignee list, not the client
// tax-consultant options: lkpm_assigned_to has its own DB CHECK, which does not
// (yet) accept Dea or Candra.
import React from "react";
import { describe, expect, it } from "vitest";

describe("LKPMBatchPage (server boundary)", () => {
  it("passes lkpmAssignees(), with neither Dea nor Candra", async () => {
    const { default: LKPMBatchPage } = await import("./page");
    const { lkpmAssignees } = await import("@/lib/workspace/roster-directory");
    const props = (
      LKPMBatchPage() as React.ReactElement<{
        taxConsultants: { value: string }[];
      }>
    ).props;
    expect(props.taxConsultants).toEqual(lkpmAssignees());
    const values = props.taxConsultants.map((c) => c.value);
    expect(values).not.toContain("dea@balizero.com");
    expect(values).not.toContain("candra@balizero.com");
  });
});

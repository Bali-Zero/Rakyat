import { describe, expect, it } from "vitest";
import type { Practice } from "@/lib/api/crm/crm.types";
import {
  assigneeLabel,
  departmentLabel,
  processAppearance,
} from "./process-appearance";

const assigned = {
  assignee_member_id: "member-a",
  assigned_to: "member@example.test",
  status: "inquiry",
} as Practice;

describe("Process appearance", () => {
  it("keeps the assignee's hue through each stage and uses full color only at completion", () => {
    const styles = [
      "inquiry",
      "waiting_documents",
      "sending_invoice",
      "on_process",
      "completed",
    ].map((status) => processAppearance({ ...assigned, status }));
    expect(new Set(styles.map((style) => style["--owner-hue"])).size).toBe(1);
    const strengths = styles.map((style) =>
      parseInt(String(style["--stage-strength"]), 10),
    );
    expect(strengths).toEqual([...new Set(strengths)].sort((a, b) => a - b));
    expect(strengths[strengths.length - 1]).toBe(100);
    expect(
      processAppearance({ ...assigned, status: "payment_pending" })[
        "--stage-strength"
      ],
    ).toBe(styles[2]["--stage-strength"]);
    expect(
      processAppearance({
        ...assigned,
        assigned_to: "new-address@example.test",
      })["--owner-hue"],
    ).toBe(styles[0]["--owner-hue"]);
    expect(
      processAppearance({ ...assigned, assignee_member_id: "member-b" })[
        "--owner-hue"
      ],
    ).not.toBe(styles[0]["--owner-hue"]);
  });

  it("uses roster slots for distinct hues independent of filtered rows and assignment aliases", () => {
    const roster = [0, 1, 2, 3, 4, 5].map((index) => ({
      ...assigned,
      assignee_color_index: index,
      assignee_member_id: `member-${index}`,
    }));
    const hues = roster.map((row) => processAppearance(row)["--owner-hue"]);
    expect(new Set(hues).size).toBe(roster.length);
    expect(hues[1]).toBeCloseTo(137.507764);
    const filtered = {
      ...roster[1],
      status: "done",
      assigned_to: "alias@example.test",
    };
    expect(processAppearance(filtered)["--owner-hue"]).toBe(hues[1]);
  });

  it("shows a neutral fallback for missing assignments and preserves legacy assignee colors", () => {
    const legacy = { ...assigned, assignee_member_id: null };
    expect(
      processAppearance({ ...legacy, assigned_to: " Member@example.test " }),
    ).toEqual(processAppearance(legacy));
    expect(
      processAppearance({ ...legacy, assigned_to: null })["--owner-chroma"],
    ).toBe(0);
    expect(
      assigneeLabel({
        ...legacy,
        assigned_to: null,
        client_lead: "other@example.test",
      }),
    ).toBe("Unassigned");
    expect(departmentLabel(legacy)).toBe("Other");
  });
});

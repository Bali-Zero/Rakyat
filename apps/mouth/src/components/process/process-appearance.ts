import type { Practice } from "@/lib/api/crm/crm.types";
import { getStatusColumn, type CaseStatus } from "./kanban-colors";

const stageStrength: Record<CaseStatus, number> = {
  inquiry: 12,
  waiting_documents: 30,
  sending_invoice: 50,
  on_process: 75,
  completed: 100,
  unknown: 12,
};

export function assigneeLabel(practice: Practice): string {
  return (
    practice.assignee_name?.trim() ||
    practice.assigned_to?.trim().split("@")[0] ||
    "Unassigned"
  );
}

export function departmentLabel(practice: Practice): string {
  if (practice.assignee_department === "setup") return "Setup";
  if (practice.assignee_department === "tax") return "Tax";
  return "Other";
}

function legacyHue(identity: string) {
  let hash = 2166136261;
  for (const character of identity) {
    hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  }
  hash = Math.imul(hash ^ (hash >>> 16), 0x85ebca6b);
  return ((hash >>> 0) / 0x100000000) * 360;
}

export function processAppearance(practice: Practice) {
  const identity =
    practice.assignee_member_id || practice.assigned_to?.trim().toLowerCase();
  const slot = practice.assignee_color_index;
  // Distribute the immutable roster slots around the color wheel, across all views.
  const hasSlot =
    typeof slot === "number" && Number.isInteger(slot) && slot >= 0;
  const hue = hasSlot
    ? (slot * 137.507764) % 360
    : identity
      ? legacyHue(identity)
      : 0;
  return {
    "--owner-hue": hue,
    "--owner-chroma": hasSlot || identity ? 0.125 : 0,
    "--stage-strength": `${stageStrength[getStatusColumn(practice.status)]}%`,
  };
}

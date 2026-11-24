"use client";

import { useState, type FormEvent } from "react";
import { ArrowRightCircle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { logger } from "@/lib/logger";
import { toError } from "@/lib/types/common";
import { ApiError } from "@/lib/api/error-handler";
import { STAGE_LABELS } from "@/lib/api/secondhome/state-machine";
import { workDayOf } from "@/lib/format/work-day";
import type {
  E33Stage,
  GuaranteeBasis,
} from "@/lib/api/secondhome/secondhome.types";
import { useAdvanceSecondHomeCase } from "@/hooks/useSecondHome";

/** Entry and ITAS activation are dated: the date anchors the Day-90 gate. */
const needsDate = (stage: string) =>
  stage === "entry" || stage === "itas_active";

/**
 * Advance-stage control. `allowedNextStages` comes from the CaseDetail
 * response's own `allowed_next_stages` field — the SERVER list is
 * authoritative (already excludes itap_eval while the flag is off); this
 * component does not recompute it from the local state-machine mirror.
 *
 * `guaranteeEvidenceComplete` (server field, 2026-08-19 reconciliation —
 * mirrors `E33Case.guarantee_evidence_complete`) is the SOLE source of
 * truth for proactively disabling guarantee_proof_due → annual_maintenance
 * when the guarantee evidence is incomplete (backend hardening rejects that
 * transition with 409 otherwise). There is no client-side recomputation of
 * this value — a prior local mirror (`guarantee-evidence.ts`) was deleted
 * per team-lead direction, since client-side mirrors of domain logic drift.
 * The server message is still surfaced verbatim on any 409 regardless.
 * `basis` is kept only to word the helper text (which proof type is meant).
 *
 * Entry and ITAS activation anchor the Day-90 gate, so their date is never
 * left to the server: the field is prefilled with today in Bali (the same
 * civil day the backend files work under, never the UTC day, which is
 * yesterday until 08:00 WITA), is required, and cannot be in the future.
 */
export function TransitionControl({
  caseId,
  currentStage,
  allowedNextStages,
  basis,
  guaranteeEvidenceComplete,
}: {
  caseId: string;
  currentStage: E33Stage;
  allowedNextStages: E33Stage[];
  basis: GuaranteeBasis;
  guaranteeEvidenceComplete: boolean;
}) {
  const toast = useToast();
  const advance = useAdvanceSecondHomeCase(caseId);
  const [toStage, setToStage] = useState<string>("");
  const [note, setNote] = useState("");
  const [occurredOn, setOccurredOn] = useState("");

  // Defensive filter — the server list is authoritative and already
  // excludes itap_eval, but never render it even if it slipped through.
  const offeredStages = allowedNextStages.filter((s) => s !== "itap_eval");

  const annualMaintenanceGated =
    currentStage === "guarantee_proof_due" && !guaranteeEvidenceComplete;

  // The stage actually submitted: the chosen one only while it is still
  // offered and enabled (a refetch may drop it from the server list).
  const effectiveTo =
    (offeredStages as string[]).includes(toStage) &&
    !(toStage === "annual_maintenance" && annualMaintenanceGated)
      ? toStage
      : "";

  if (offeredStages.length === 0) {
    return (
      <div className="rounded-xl border border-[var(--bz-border)] bg-[var(--bz-card)] p-4">
        <h3 className="text-sm font-semibold text-[var(--bz-text-1)] mb-1">
          Advance Stage
        </h3>
        <p className="text-xs text-[var(--bz-text-2)]">
          {STAGE_LABELS[currentStage]} is a terminal stage — no further
          transitions.
        </p>
      </div>
    );
  }

  const needsOccurredOn = needsDate(effectiveTo);
  const today = workDayOf();
  // ISO days compare correctly as strings.
  const dateInFuture = needsOccurredOn && occurredOn > today;
  const dateInvalid = needsOccurredOn && (!occurredOn || dateInFuture);

  const handleStageChange = (stage: string) => {
    setToStage(stage);
    if (needsDate(stage) && !occurredOn) setOccurredOn(workDayOf());
  };

  const handleAdvance = async (e: FormEvent) => {
    e.preventDefault();
    if (!effectiveTo) return;
    // Re-read the day at submit: the form may have sat open past midnight.
    if (needsOccurredOn && (!occurredOn || occurredOn > workDayOf())) return;
    try {
      await advance.mutateAsync({
        to_stage: effectiveTo as E33Stage,
        note: note || undefined,
        occurred_on: needsOccurredOn ? occurredOn : undefined,
      });
      toast.success(
        "Stage Advanced",
        `Case moved to ${STAGE_LABELS[effectiveTo as E33Stage]}.`,
      );
      setToStage("");
      setNote("");
      setOccurredOn("");
    } catch (error) {
      logger.error(
        "Failed to advance second-home case",
        { component: "TransitionControl", action: "advance", itemId: caseId },
        toError(error),
      );
      // Surface the 409/422 detail verbatim — it names the allowed set or
      // the itap_eval gate reason.
      const message =
        error instanceof ApiError
          ? error.detail || error.message
          : "Failed to advance stage";
      toast.error("Error", message);
    }
  };

  return (
    <form
      onSubmit={handleAdvance}
      className="rounded-xl border border-[var(--bz-border)] bg-[var(--bz-card)] p-4 space-y-3"
    >
      <h3 className="text-sm font-semibold text-[var(--bz-text-1)]">
        Advance Stage
      </h3>
      <div className="space-y-2">
        <select
          value={effectiveTo}
          onChange={(e) => handleStageChange(e.target.value)}
          className="w-full px-3 py-2 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-base)] text-[var(--bz-text-1)] focus:outline-none focus:ring-2 focus:ring-[var(--bz-accent)]/50"
        >
          <option value="">-- Select next stage --</option>
          {offeredStages.map((stage) => (
            <option
              key={stage}
              value={stage}
              disabled={
                stage === "annual_maintenance" && annualMaintenanceGated
              }
            >
              {STAGE_LABELS[stage]}
              {stage === "annual_maintenance" && annualMaintenanceGated
                ? " (evidence incomplete)"
                : ""}
            </option>
          ))}
        </select>
        {annualMaintenanceGated && (
          <p className="text-xs text-[var(--bz-text-2)]">
            Annual Maintenance requires the guarantee evidence to be complete —
            basis proof (
            {basis === "deposit" ? "bank confirmation" : "property title"}) plus
            a filed immigration filing. Add evidence first.
          </p>
        )}
        {needsOccurredOn && (
          <div>
            <label className="text-xs text-[var(--bz-text-2)] block mb-1">
              {effectiveTo === "entry" ? "Entry date" : "ITAS activation date"}{" "}
              <span className="text-[var(--bz-text-2)]">
                (required — today in Bali unless you change it; it anchors the
                Day-90 gate and cannot be in the future)
              </span>
            </label>
            <input
              type="date"
              value={occurredOn}
              max={today}
              required
              aria-invalid={dateInvalid || undefined}
              onChange={(e) => setOccurredOn(e.target.value)}
              className="w-full px-3 py-2 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-base)] text-[var(--bz-text-1)] focus:outline-none focus:ring-2 focus:ring-[var(--bz-accent)]/50"
            />
            {dateInFuture && (
              <p
                role="alert"
                className="text-xs text-[var(--state-danger)] mt-1"
              >
                The date cannot be in the future.
              </p>
            )}
          </div>
        )}
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Note (optional)"
          rows={2}
          className="w-full px-3 py-2 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-base)] text-[var(--bz-text-1)] placeholder:text-[var(--bz-text-2)] focus:outline-none focus:ring-2 focus:ring-[var(--bz-accent)]/50 resize-none"
        />
      </div>
      <Button
        type="submit"
        disabled={!effectiveTo || dateInvalid || advance.isPending}
        className="w-full gap-2 bg-[var(--bz-accent)] text-[var(--accent-foreground)] hover:bg-[var(--bz-accent)]/90"
      >
        {advance.isPending ? (
          <Loader2 className="w-4 h-4 animate-spin" />
        ) : (
          <ArrowRightCircle className="w-4 h-4" />
        )}
        Advance
      </Button>
    </form>
  );
}

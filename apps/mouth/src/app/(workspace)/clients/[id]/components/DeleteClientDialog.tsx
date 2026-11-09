"use client";

import React, { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * CRM-18: deleting a client takes two deliberate steps.
 *
 *   1. Say what happens: the record is marked inactive, not erased.
 *   2. Type the client's name to enable the final button.
 *
 * The delete itself stays a soft delete (DELETE /api/crm/clients/{id} sets
 * status inactive + deleted_at); this dialog only guards the click.
 */
export function DeleteClientDialog({
  open,
  clientName,
  isDeleting,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  clientName: string;
  isDeleting: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [typed, setTyped] = useState("");
  const nameMatches =
    typed.trim().toLowerCase() === clientName.trim().toLowerCase() &&
    typed.trim() !== "";

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      setStep(1);
      setTyped("");
    }
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {step === 1 ? "Delete this client?" : "Confirm deletion"}
          </DialogTitle>
          <DialogDescription>
            {step === 1
              ? `"${clientName}" will be marked inactive and removed from client lists. The record is not erased.`
              : `Type the client's name to confirm.`}
          </DialogDescription>
        </DialogHeader>

        {step === 2 ? (
          <div className="space-y-2">
            <label
              htmlFor="delete-client-confirm"
              className="text-sm text-[var(--tx-secondary)]"
            >
              Client name
            </label>
            <input
              id="delete-client-confirm"
              autoComplete="off"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={clientName}
              className="w-full rounded-md border border-[var(--bz-border)] bg-transparent px-3 py-2 text-sm"
            />
          </div>
        ) : null}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => handleOpenChange(false)}
            disabled={isDeleting}
          >
            Cancel
          </Button>
          {step === 1 ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => setStep(2)}
            >
              Continue
            </Button>
          ) : (
            <Button
              type="button"
              variant="outline"
              onClick={onConfirm}
              disabled={!nameMatches || isDeleting}
            >
              {isDeleting ? "Deleting…" : "Delete client"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

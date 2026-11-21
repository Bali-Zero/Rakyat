"use client";

/**
 * The one lock of the reconciliation tab: ONE WRITE AT A TIME.
 *
 * Truth lives in two refs, `pending` (a write request is in flight) and
 * `dialog` (the confirm dialog is open); every operation decides on the refs at
 * call time, so two events in the same tick cannot both pass. The state copy
 * exists only to re-render the buttons. A refused operation does nothing.
 */

import { useCallback, useMemo, useRef, useState } from "react";

export function useWriteLock() {
  const pendingRef = useRef(false);
  const dialogRef = useRef(false);
  const [view, setView] = useState({ pending: false, dialog: false });

  const sync = useCallback(() => {
    setView({ pending: pendingRef.current, dialog: dialogRef.current });
  }, []);

  const tryOpenDialog = useCallback((): boolean => {
    if (pendingRef.current || dialogRef.current) return false;
    dialogRef.current = true;
    sync();
    return true;
  }, [sync]);

  const tryCloseDialog = useCallback((): boolean => {
    if (!dialogRef.current || pendingRef.current) return false;
    dialogRef.current = false;
    sync();
    return true;
  }, [sync]);

  // pending is set BEFORE the first await and cleared in finally.
  const run = useCallback(
    async (fn: () => Promise<unknown>): Promise<boolean> => {
      pendingRef.current = true;
      sync();
      try {
        await fn();
      } finally {
        pendingRef.current = false;
        sync();
      }
      return true;
    },
    [sync],
  );

  const runWrite = useCallback(
    async (fn: () => Promise<unknown>): Promise<boolean> => {
      if (pendingRef.current || dialogRef.current) return false;
      return run(fn);
    },
    [run],
  );

  const runDialogWrite = useCallback(
    async (fn: () => Promise<unknown>): Promise<boolean> => {
      if (!dialogRef.current || pendingRef.current) return false;
      return run(fn);
    },
    [run],
  );

  const dialogDone = useCallback((): void => {
    dialogRef.current = false;
    sync();
  }, [sync]);

  /** Neither a write in flight nor the dialog open (reads the refs, never a render copy). */
  const isFree = useCallback(
    (): boolean => !pendingRef.current && !dialogRef.current,
    [],
  );

  return useMemo(
    () => ({
      locked: view.pending || view.dialog,
      pending: view.pending,
      isFree,
      tryOpenDialog,
      tryCloseDialog,
      runWrite,
      runDialogWrite,
      dialogDone,
    }),
    [
      view,
      isFree,
      tryOpenDialog,
      tryCloseDialog,
      runWrite,
      runDialogWrite,
      dialogDone,
    ],
  );
}

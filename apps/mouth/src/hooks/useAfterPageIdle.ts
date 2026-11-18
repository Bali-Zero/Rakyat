"use client";

import { useEffect, useState } from "react";

/**
 * False until the page has finished loading AND the browser reports an idle
 * moment, then true for the life of the component.
 *
 * For data that is not part of the first screen (e.g. the layout-wide Visa
 * Oracle testing overlay): gating its query on this keeps it from competing
 * with the open page's own requests during first load. `requestIdleCallback`
 * where available (with a `maxWaitMs` cap so it always fires), else a short
 * timeout after `load`.
 */
export function useAfterPageIdle(maxWaitMs = 3000): boolean {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (ready) return;
    let idleId: number | null = null;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    const scheduleIdle = () => {
      if (typeof window.requestIdleCallback === "function") {
        idleId = window.requestIdleCallback(() => setReady(true), {
          timeout: maxWaitMs,
        });
      } else {
        timeoutId = setTimeout(() => setReady(true), Math.min(maxWaitMs, 1000));
      }
    };

    if (document.readyState === "complete") {
      scheduleIdle();
    } else {
      window.addEventListener("load", scheduleIdle, { once: true });
    }

    return () => {
      window.removeEventListener("load", scheduleIdle);
      if (idleId !== null && typeof window.cancelIdleCallback === "function") {
        window.cancelIdleCallback(idleId);
      }
      if (timeoutId !== null) clearTimeout(timeoutId);
    };
  }, [ready, maxWaitMs]);

  return ready;
}

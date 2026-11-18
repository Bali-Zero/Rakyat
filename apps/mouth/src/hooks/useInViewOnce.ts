"use client";

import { useEffect, useState, type RefObject } from "react";

/**
 * True once the referenced element has come within `rootMargin` of the
 * viewport, and stays true. Where IntersectionObserver is unavailable it is
 * true at once, so content never stays unloaded.
 */
export function useInViewOnce(
  ref: RefObject<Element | null>,
  rootMargin = "200px",
): boolean {
  const [seen, setSeen] = useState(
    () => typeof IntersectionObserver === "undefined",
  );

  useEffect(() => {
    if (seen) return;
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setSeen(true);
          observer.disconnect();
        }
      },
      { rootMargin },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, rootMargin, seen]);

  return seen;
}

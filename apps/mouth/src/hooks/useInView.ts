"use client";

import { useEffect, useState, type RefObject } from "react";

/**
 * True while the element intersects the viewport, false otherwise — it keeps
 * observing (unlike `useInViewOnce`, which fires once and disconnects).
 *
 * Living loops need both edges: start when the element enters the viewport,
 * pause when it leaves, so an off-screen video never keeps decoding (battery
 * on mobile) and never counts against "one peak per viewport" (design R3).
 *
 * Without IntersectionObserver (old browsers, SSR) it stays false: the
 * caller shows its static poster, which is the safe failure.
 */
export function useInView(
  ref: RefObject<Element | null>,
  { rootMargin = "0px", threshold = 0.25 }: { rootMargin?: string; threshold?: number } = {},
): boolean {
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const last = entries[entries.length - 1];
        if (last) setInView(last.isIntersecting && last.intersectionRatio >= threshold);
      },
      { rootMargin, threshold: [0, threshold] },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, rootMargin, threshold]);

  return inView;
}

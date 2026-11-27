"use client";

import { useEffect, useRef } from "react";
import type { Language } from "../_lib/flow";
import {
  getHistoryNavigationType,
  localeFromLocationSearch,
  reconcileVisitLocaleOnClient,
  writeVisitLocaleHint,
} from "../_lib/oracle-locale";

export function useOracleVisitLocale(
  language: Language,
  setLanguage: (next: Language) => void,
): void {
  const languageRef = useRef(language);
  languageRef.current = language;

  useEffect(() => {
    const reconcileTraversal = (opts?: {
      pageshowPersisted?: boolean;
      popstate?: boolean;
    }) => {
      const effective = reconcileVisitLocaleOnClient(languageRef.current, opts);
      if (effective !== languageRef.current) {
        setLanguage(effective);
      }
    };

    const navigationType = getHistoryNavigationType();
    if (navigationType === "back_forward" || navigationType === "reload") {
      reconcileTraversal();
    } else if (navigationType === "navigate") {
      writeVisitLocaleHint(localeFromLocationSearch(window.location.search));
    }

    const onPopState = () => reconcileTraversal({ popstate: true });
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        reconcileTraversal({ pageshowPersisted: true });
      }
    };

    window.addEventListener("popstate", onPopState);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("pageshow", onPageShow);
    };
  }, [setLanguage]);
}

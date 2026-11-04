"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api";
import { isOwner } from "@/lib/auth/owner";

export type OwnerGate = "loading" | "owner" | "forbidden";

/**
 * Page-level owner gate for the owner-cashout surfaces.
 *
 * The HR nav already hides the entry for everyone but the owner, and every
 * backend route answers 403 — this closes the third door, the URL: a global
 * admin who types /hr/owner-cashout gets nothing rendered and is sent back
 * to /hr. The profile is read the same way the HR layout reads it; a failed
 * read is treated as "not the owner", never as "owner".
 */
export function useOwnerGate(fallbackHref = "/hr"): OwnerGate {
  const router = useRouter();
  const [gate, setGate] = useState<OwnerGate>("loading");

  useEffect(() => {
    let mounted = true;
    api
      .getProfile()
      .then((user) => {
        if (mounted) setGate(isOwner(user?.email) ? "owner" : "forbidden");
      })
      .catch(() => {
        if (mounted) setGate("forbidden");
      });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    if (gate === "forbidden") router.replace(fallbackHref);
  }, [gate, router, fallbackHref]);

  return gate;
}

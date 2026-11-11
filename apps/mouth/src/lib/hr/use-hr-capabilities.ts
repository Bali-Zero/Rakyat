"use client";

import { useEffect, useState } from "react";

import { getCapabilities, type HrCapabilities } from "@/lib/api/hr/hr";

/** A failed or pending read grants nothing: the member view is the floor. */
export const NO_CAPABILITIES: HrCapabilities = {
  hr_admin: false,
  accounting_admin: false,
  has_employee_record: false,
};

export interface HrCapabilitiesState {
  capabilities: HrCapabilities;
  loaded: boolean;
}

/**
 * What the signed-in member may see in the HR & accounting space, as answered
 * by the backend predicates that guard the routes themselves.
 */
export function useHrCapabilities(): HrCapabilitiesState {
  const [state, setState] = useState<HrCapabilitiesState>({
    capabilities: NO_CAPABILITIES,
    loaded: false,
  });

  useEffect(() => {
    let mounted = true;
    getCapabilities()
      .then((capabilities) => {
        if (mounted) setState({ capabilities, loaded: true });
      })
      .catch(() => {
        if (mounted) setState({ capabilities: NO_CAPABILITIES, loaded: true });
      });
    return () => {
      mounted = false;
    };
  }, []);

  return state;
}

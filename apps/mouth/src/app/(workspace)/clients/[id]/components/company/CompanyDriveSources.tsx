"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CompanyDriveSources as DriveSources } from "@/lib/api/crm/crm.types";
import { EYEBROW, FOCUS } from "@/components/workspace/r19";

const LABELS = {
  primary: "Primary",
  restricted_tax: "Restricted tax archive",
  legacy: "Legacy archive",
};

export function CompanyDriveSources({ companyId }: { companyId: number }) {
  const [data, setData] = useState<DriveSources | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    setData(null);
    setFailed(false);
    async function load() {
      try {
        const result = await api.crm.getCompanyDriveSources(companyId);
        if (active) setData(result);
      } catch {
        if (active) setFailed(true);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [companyId]);

  // Never display the previous company's links during a selection change.
  const current = data?.company_id === companyId ? data : null;
  return (
    <div
      role="region"
      aria-label="Company Drive sources"
      className="pb-4 text-sm text-[var(--tx-secondary)]"
    >
      <h3 className={EYEBROW}>Drive sources</h3>
      {failed ? (
        <p role="status" className="mt-2">
          Drive sources are unavailable.
        </p>
      ) : !current ? (
        <p role="status" className="mt-2">
          Loading Drive sources…
        </p>
      ) : (
        <>
          {current.sources.length ? (
            <ul className="mt-2 flex flex-col gap-2">
              {current.sources.map((source) => (
                <li key={source.url}>
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={`${FOCUS} text-[var(--tx-pure)] underline underline-offset-4`}
                  >
                    {source.kinds.map((kind) => LABELS[kind]).join(" · ")}
                  </a>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2">No Drive sources recorded.</p>
          )}
          {current.sources.length > 0 && (
            <p className="mt-2 text-[12px]">
              Opens Google Drive. Existing Drive permissions apply.
            </p>
          )}
          {current.historical_record_ids.map((id) => (
            <p className="mt-2" key={id}>
              Historical record #{id}
            </p>
          ))}
          {current.lineage_status === "valid" &&
            current.merged_into_company_id !== null && (
              <p className="mt-2">
                Historical record #{companyId} · merged into record #
                {current.merged_into_company_id}
              </p>
            )}
          {current.lineage_status === "invalid" && (
            <p className="mt-2">Historical reference unavailable.</p>
          )}
        </>
      )}
    </div>
  );
}

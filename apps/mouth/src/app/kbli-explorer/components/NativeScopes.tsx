import type { KBLINativeScopeView } from "@/lib/api/kbli.api";
import { safeHtml } from "@/lib/utils/safe-html";
import {
  describeObligation,
  TRUNCATION_HINT,
  TRUNCATION_NOTE,
} from "@/lib/kbli-obligation-truncation";

export function scopeCoverageLabel(view: KBLINativeScopeView): string {
  if (view.status === "available") return "Risk by scope";
  if (view.status === "verified_empty")
    return "No scopes in the verified source";
  return "Scope coverage not verified";
}

function ScopeTerms({ label, terms }: { label: string; terms: string[] }) {
  return (
    <div className="mt-3">
      <h4 className="text-xs font-semibold text-silver">{label}</h4>
      {terms.length ? (
        <ul className="list-disc space-y-1 pl-5">
          {terms.map((text, i) => {
            const duty = describeObligation(text);
            return (
              <li key={i}>
                <div
                  dangerouslySetInnerHTML={safeHtml(duty.text, {
                    ALLOWED_TAGS: [
                      "p",
                      "br",
                      "strong",
                      "b",
                      "em",
                      "ol",
                      "ul",
                      "li",
                    ],
                    ALLOWED_ATTR: [],
                    ALLOW_DATA_ATTR: false,
                  })}
                />
                {duty.truncated && (
                  <span title={TRUNCATION_HINT}>[…{TRUNCATION_NOTE}]</span>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p>None listed for this row.</p>
      )}
    </div>
  );
}

export default function NativeScopes({
  view,
  note,
}: {
  view: KBLINativeScopeView;
  note?: string | null;
}) {
  return (
    <section
      aria-label="Licensing by scope"
      className="space-y-3 text-sm text-[var(--foreground)]"
    >
      <h3 className="text-xs font-bold uppercase tracking-widest text-[var(--kbli-text-muted)]">
        Licensing by scope
      </h3>
      {view.status === "available" && note && (
        <p
          aria-label="Licensing source note"
          className="text-xs text-[var(--kbli-text-muted)]"
        >
          {note}
        </p>
      )}
      {view.status === "available" ? (
        view.rows.map((row) => (
          <details
            key={row.row_ordinal}
            className="rounded border border-[var(--border)] p-3"
          >
            <summary className="cursor-pointer font-medium">
              {row.scope_uraian} · {row.scale.join(", ")} · {row.risk_level}
            </summary>
            <p className="mt-3">Licence: {row.type}</p>
            <ScopeTerms label="Requirements" terms={row.requirements} />
            <ScopeTerms label="Obligations" terms={row.obligations} />
          </details>
        ))
      ) : (
        <p>{scopeCoverageLabel(view)}.</p>
      )}
    </section>
  );
}

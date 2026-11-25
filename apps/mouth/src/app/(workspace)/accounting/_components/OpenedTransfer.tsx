import { Loader2 } from "lucide-react";
import type {
  MatchCandidate,
  MatchCandidatesResponse,
  ReconciliationInboxItem,
} from "@/lib/api/workspace/accounting.api";
import { candidateIdentityParts, NO_INVOICE_NUMBER } from "./CandidateIdentity";
import { CcAmount } from "./CcAmount";
import { ccFull, ccInv, ccShort } from "./ccFormat";
import {
  CcGlyph,
  CC_PRIMARY,
  CC_SECONDARY,
  CC_TEXT,
  type CcGlyphName,
} from "./CcGlyph";
import { SITUATION_CLASS, type Situation } from "./ReconciliationTabs";

export type CandidatePick = {
  txnId: number;
  group: "open" | "recorded";
  candidate: MatchCandidate;
};

const PICKED_ROW = "bg-[var(--cc-sel)]";

const BAND: Record<Situation, { title: string; glyph: CcGlyphName }> = {
  confirm: { title: "One invoice matches this transfer", glyph: "confirm" },
  link: { title: "This payment looks already recorded", glyph: "link" },
  choose: { title: "Which invoice is this payment for?", glyph: "choose" },
  nomatch: { title: "No invoice has this amount", glyph: "nomatch" },
};

/** "Rp Y of Rp X still to match": a transfer partly applied by earlier confirmations. */
function StillToMatch({ item }: { item: ReconciliationInboxItem }) {
  const applied = item.applied_idr ?? 0;
  if (applied <= 0) return null;
  return (
    <p className="text-sm text-[var(--state-warning)]">
      <CcAmount value={item.amount_idr - applied} /> of{" "}
      <CcAmount value={item.amount_idr} /> still to match
    </p>
  );
}

function BankSide({ item }: { item: ReconciliationInboxItem }) {
  return (
    <div className="rounded-[12px] bg-[var(--cc-wash)] px-6 pb-6 pt-[22px]">
      <p className="text-xs font-semibold text-[var(--cc-tx-2)]">
        From the bank
      </p>
      <span className="mb-4 mt-2 block text-[26px] font-medium leading-[1.1]">
        <CcAmount value={item.amount_idr} />
      </span>
      <StillToMatch item={item} />
      <p className="text-sm font-bold">{item.description || "(no memo)"}</p>
      <p className="mt-0.5 text-sm text-[var(--cc-tx-2)]">
        Received {ccFull(item.txn_date)}
      </p>
    </div>
  );
}

function PairView({
  item,
  pick,
  situation,
}: {
  item: ReconciliationInboxItem;
  pick: CandidatePick;
  situation: Situation;
}) {
  const identity = candidateIdentityParts(pick.candidate);
  const secondary = [
    identity.service,
    identity.date ? `issued ${ccShort(identity.date)}` : null,
  ].filter((part): part is string => part !== null);
  return (
    <>
      <div className="grid grid-cols-[1fr_40px_1fr] items-stretch">
        <BankSide item={item} />
        <span className="mx-auto flex h-[30px] w-[30px] items-center justify-center self-center rounded-full bg-[var(--cc-solid)] font-serif text-lg font-semibold leading-none text-[var(--cc-on)]">
          =
        </span>
        <div className="rounded-[12px] border border-[var(--cc-edge)] px-6 pb-6 pt-[22px]">
          <p className="text-xs font-semibold text-[var(--cc-tx-2)]">
            Our invoice
          </p>
          <span className="mb-4 mt-2 block text-[26px] font-medium leading-[1.1]">
            <CcAmount value={pick.candidate.invoice_amount_idr} />
          </span>
          <p className="text-sm font-bold">
            {identity.number ?? NO_INVOICE_NUMBER} ·{" "}
            {pick.candidate.client_name}
          </p>
          {secondary.length > 0 && (
            <p className="mt-0.5 text-sm text-[var(--cc-tx-2)]">
              {secondary.join(" · ")}
            </p>
          )}
          {situation === "link" && (
            <p className="mt-0.5 text-sm text-[var(--cc-tx-2)]">Already paid</p>
          )}
        </div>
      </div>
      <p className="mt-[22px] flex items-center gap-2.5 text-base">
        <CcGlyph
          name="tick"
          className="h-5 w-5 flex-none text-[var(--cc-ink)]"
        />
        {pick.candidate.reasons.join(" · ")}
      </p>
      {situation === "link" && (
        <p className="mt-2 text-sm text-[var(--cc-tx-2)]">
          Link it instead of confirming it again.
        </p>
      )}
    </>
  );
}

function CandidateGroup({
  title,
  rows,
  count,
  group,
  item,
  situation,
  pick,
  onPick,
}: {
  title: string;
  rows: MatchCandidate[];
  count: number;
  group: CandidatePick["group"];
  item: ReconciliationInboxItem;
  situation: Situation;
  pick: CandidatePick | null;
  onPick: (pick: CandidatePick) => void;
}) {
  const truncated = count > rows.length;
  if (rows.length === 0 && !truncated) return null;
  return (
    <div>
      <h3 className="mt-3.5 text-xs font-semibold text-[var(--cc-tx-2)]">
        {title}
      </h3>
      {rows.length > 0 && (
        <div className="mt-3 overflow-hidden rounded-[12px] border border-[var(--cc-edge)]">
          {rows.map((candidate) => {
            const identity = candidateIdentityParts(candidate);
            const picked =
              pick?.group === group &&
              pick.candidate.invoice_id === candidate.invoice_id;
            const suggested =
              item.suggestion?.invoice_id === candidate.invoice_id &&
              ((situation === "confirm" && group === "open") ||
                (situation === "link" && group === "recorded"));
            return (
              <label
                key={`${group}-${candidate.invoice_id}`}
                data-testid={`cc-cand-${candidate.invoice_id}`}
                className={`grid min-h-10 grid-cols-[18px_150px_minmax(50px,auto)_minmax(0,1fr)_minmax(0,1.2fr)_auto] items-center gap-x-3.5 border-b border-[var(--cc-hair)] px-[18px] py-1 text-sm last:border-b-0 ${picked ? PICKED_ROW : ""}`}
              >
                <input
                  type="radio"
                  name={`cc-pick-${item.txn_id}`}
                  checked={picked}
                  onChange={() =>
                    onPick({ txnId: item.txn_id, group, candidate })
                  }
                  className="h-[18px] w-[18px] appearance-none rounded-full border-[1.5px] border-[var(--cc-line-control)] bg-[var(--cc-card)] checked:border-[5.5px] checked:border-[var(--cc-ink)]"
                />
                <span className="font-serif font-semibold">
                  {identity.number ?? NO_INVOICE_NUMBER}
                </span>
                <span className="text-[var(--cc-tx-2)]">
                  {identity.date ? ccInv(identity.date, item.txn_date) : ""}
                </span>
                <span className="truncate">{identity.service ?? ""}</span>
                <span className="truncate font-semibold">
                  {candidate.client_name}
                  {suggested && (
                    <span className="ml-2 rounded bg-[var(--cc-pill)] px-1.5 py-0.5 text-[10px] font-semibold uppercase text-[var(--cc-ink)]">
                      Suggested
                    </span>
                  )}
                </span>
                <span className="text-right font-medium">
                  <CcAmount value={candidate.invoice_amount_idr} />
                </span>
                {candidate.reasons.length > 0 && (
                  <span
                    data-testid={`cc-reasons-${candidate.invoice_id}`}
                    className="col-span-5 col-start-2 text-xs leading-4 text-[var(--cc-tx-2)] [overflow-wrap:anywhere]"
                  >
                    {candidate.reasons.join(" · ")}
                  </span>
                )}
              </label>
            );
          })}
        </div>
      )}
      {truncated && (
        <p className="mt-1 text-xs text-[var(--cc-tx-2)]">
          {count} invoices have this amount; the {rows.length} closest are
          shown.
        </p>
      )}
    </div>
  );
}

function TableGroups({
  item,
  situation,
  candidates,
  pick,
  onPick,
}: {
  item: ReconciliationInboxItem;
  situation: Situation;
  candidates: MatchCandidatesResponse;
  pick: CandidatePick | null;
  onPick: (pick: CandidatePick) => void;
}) {
  const empty =
    candidates.candidates.length === 0 &&
    candidates.recorded_candidates.length === 0 &&
    item.open_candidates <= 0 &&
    item.recorded_candidates <= 0;
  return (
    <>
      {empty && (
        <p className="py-2 text-center text-sm text-[var(--cc-tx-2)]">
          No plausible invoice match. Leave unmatched or handle manually.
        </p>
      )}
      <CandidateGroup
        title="Open invoices"
        rows={candidates.candidates}
        count={item.open_candidates}
        group="open"
        item={item}
        situation={situation}
        pick={pick}
        onPick={onPick}
      />
      <CandidateGroup
        title="Already paid"
        rows={candidates.recorded_candidates}
        count={item.recorded_candidates}
        group="recorded"
        item={item}
        situation={situation}
        pick={pick}
        onPick={onPick}
      />
    </>
  );
}

export function OpenedTransfer({
  situation,
  item,
  rowsCount,
  candidates,
  loading,
  failed,
  pair,
  nothing,
  pick,
  ignoreForm,
  reasonOk,
  locked,
  rowError,
  onRetry,
  onChooseAnother,
  onPick,
  onConfirm,
  onLink,
  onSkip,
  onIgnore,
  onIgnoreReason,
  onSendIgnore,
  onCancelIgnore,
}: {
  situation: Situation;
  item: ReconciliationInboxItem;
  rowsCount: number;
  candidates: MatchCandidatesResponse | null;
  loading: boolean;
  failed: boolean;
  pair: boolean;
  nothing: boolean;
  pick: CandidatePick | null;
  ignoreForm: { txnId: number; reason: string } | null;
  reasonOk: boolean;
  locked: boolean;
  rowError: string | null;
  onRetry: () => void;
  onChooseAnother: () => void;
  onPick: (pick: CandidatePick) => void;
  onConfirm: () => void;
  onLink: () => void;
  onSkip: () => void;
  onIgnore: () => void;
  onIgnoreReason: (value: string) => void;
  onSendIgnore: () => void;
  onCancelIgnore: () => void;
}) {
  const formOpen = ignoreForm?.txnId === item.txn_id;
  const table = !pair && !nothing && candidates !== null;
  const note =
    pick?.group === "recorded"
      ? "Linking adds this transfer to the cash-out and dates the invoice; the case does not change. You can reopen this transfer any time from Done today."
      : nothing
        ? "Ignoring moves no money. You can reopen this transfer any time from Done today."
        : "Confirming records this payment on the practice, the invoice and the cashout. You can undo it today from Done today. Paid in full, a case waiting for payment moves to On Process.";
  const primaryLabel =
    pick?.group === "recorded" ? "Link payment" : "Confirm payment";
  const noteGlyph = pick?.group === "recorded" || nothing ? "undo" : "lock";
  return (
    <section
      data-testid="cc-opened"
      aria-label="Opened transfer"
      className={`${SITUATION_CLASS[situation]} flex min-h-0 flex-col overflow-hidden rounded-[14px] border border-[var(--cc-edge)] bg-[var(--cc-card)] text-[var(--cc-tx)]`}
    >
      <div
        data-testid="cc-band"
        className="flex h-16 flex-none items-center gap-3.5 bg-[var(--cc-solid)] px-7 text-[var(--cc-on)]"
      >
        <CcGlyph
          name={BAND[situation].glyph}
          className="h-[26px] w-[26px] flex-none"
        />
        <h2 className="text-xl font-semibold">{BAND[situation].title}</h2>
      </div>
      <div
        className={
          table
            ? "flex min-h-0 flex-1 flex-col px-7 py-5"
            : "flex min-h-0 flex-1 flex-col px-7 pb-6 pt-[26px]"
        }
      >
        {table && (
          <div className="grid flex-none grid-cols-[1fr_auto] items-center gap-5 rounded-[12px] bg-[var(--cc-wash)] px-[22px] py-3">
            <div>
              <p className="text-xs font-semibold text-[var(--cc-tx-2)]">
                From the bank
              </p>
              <p className="text-sm font-bold">
                {item.description || "(no memo)"}
              </p>
              <p className="text-sm text-[var(--cc-tx-2)]">
                Received {ccFull(item.txn_date)}
              </p>
              <StillToMatch item={item} />
            </div>
            <span className="text-[26px] font-medium leading-none">
              <CcAmount value={item.amount_idr} />
            </span>
          </div>
        )}
        <div data-testid="cc-scroll" className="min-h-0 flex-1 overflow-y-auto">
          {failed ? (
            <div>
              <p className="text-sm text-[var(--state-danger)]">
                Could not load the invoices for this transfer.
              </p>
              <button
                type="button"
                className={`${CC_SECONDARY} mt-3`}
                onClick={onRetry}
              >
                Try again
              </button>
            </div>
          ) : loading || candidates === null ? (
            <div className="flex items-center justify-center py-4 text-[var(--cc-tx-2)]">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Ranking
              candidates…
            </div>
          ) : pair && pick ? (
            <PairView item={item} pick={pick} situation={situation} />
          ) : nothing ? (
            <div className="grid grid-cols-[1fr_40px_1fr] items-stretch">
              <BankSide item={item} />
              <span className="mx-auto flex h-[30px] w-[30px] items-center justify-center self-center rounded-full bg-[var(--cc-solid)] font-serif text-lg font-semibold leading-none text-[var(--cc-on)]">
                ≠
              </span>
              <div className="rounded-[12px] border-[1.5px] border-dashed border-[var(--cc-line-control)] px-6 pb-6 pt-[22px]">
                <p className="text-xs font-semibold text-[var(--cc-tx-2)]">
                  Our side
                </p>
                <p className="mb-3.5 mt-2.5 text-xl font-bold leading-[1.2]">
                  Nothing found
                </p>
                <p className="text-sm">
                  No open or paid invoice of{" "}
                  <CcAmount value={item.amount_idr} />.
                </p>
                <p className="mt-0.5 text-sm text-[var(--cc-tx-2)]">
                  If it is not a client payment, ignore it with a reason.
                </p>
              </div>
            </div>
          ) : (
            <TableGroups
              item={item}
              situation={situation}
              candidates={candidates}
              pick={pick}
              onPick={onPick}
            />
          )}
        </div>

        {formOpen && (
          <div
            data-testid="cc-ignore-form"
            className="mt-[22px] flex flex-wrap items-center gap-2.5"
          >
            <p className="basis-full text-base font-semibold">
              Not a client payment? Write the reason.
            </p>
            <input
              type="text"
              aria-label="Reason for ignoring"
              placeholder="Why is this not a client payment?"
              value={ignoreForm.reason}
              disabled={locked}
              onChange={(event) => onIgnoreReason(event.target.value)}
              className="h-10 min-w-[240px] flex-1 rounded-[10px] border border-[var(--cc-line-control)] bg-[var(--cc-card)] px-3 text-sm text-[var(--cc-tx)]"
            />
            <button
              type="button"
              className={CC_PRIMARY}
              disabled={locked || !reasonOk}
              onClick={onSendIgnore}
            >
              Ignore transfer
            </button>
            <button
              type="button"
              className={CC_TEXT}
              disabled={locked}
              onClick={onCancelIgnore}
            >
              Cancel
            </button>
            <p className="basis-full text-sm text-[var(--cc-tx-2)]">
              Ignoring moves no money. You can reopen this transfer any time
              from Done today.
            </p>
          </div>
        )}

        {!formOpen && !loading && !failed && candidates !== null && (
          <div
            data-testid="cc-foot"
            className={
              table
                ? "mt-4 flex flex-none items-center gap-4 border-t border-[var(--cc-hair)] pt-4"
                : "mt-[26px] flex flex-none items-center gap-4 border-t border-[var(--cc-hair)] pt-[22px]"
            }
          >
            <p className="flex min-w-0 flex-1 items-start gap-2 text-sm text-[var(--cc-tx-2)] [text-wrap:pretty]">
              <CcGlyph name={noteGlyph} className="mt-0.5 h-4 w-4 flex-none" />
              {note}
            </p>
            <div className="ml-auto flex flex-none items-center gap-1">
              {pair ? (
                <>
                  <button
                    type="button"
                    className={CC_TEXT}
                    disabled={locked}
                    onClick={onChooseAnother}
                  >
                    Choose another
                  </button>
                  <button
                    type="button"
                    className={CC_TEXT}
                    disabled={locked}
                    onClick={onIgnore}
                  >
                    Ignore
                  </button>
                  <button
                    type="button"
                    className={CC_PRIMARY}
                    disabled={locked}
                    onClick={situation === "link" ? onLink : onConfirm}
                  >
                    {situation === "link" ? "Link payment" : "Confirm payment"}
                  </button>
                </>
              ) : nothing ? (
                <>
                  <button
                    type="button"
                    className={CC_TEXT}
                    disabled={locked || rowsCount === 1}
                    onClick={onSkip}
                  >
                    Skip for now
                  </button>
                  <button
                    type="button"
                    className={CC_PRIMARY}
                    disabled={locked}
                    onClick={onIgnore}
                  >
                    Ignore with a reason
                  </button>
                </>
              ) : table ? (
                <>
                  <button
                    type="button"
                    className={CC_TEXT}
                    disabled={locked || rowsCount === 1}
                    onClick={onSkip}
                  >
                    Skip for now
                  </button>
                  <button
                    type="button"
                    className={CC_TEXT}
                    disabled={locked}
                    onClick={onIgnore}
                  >
                    Ignore
                  </button>
                  <button
                    type="button"
                    className={CC_PRIMARY}
                    disabled={locked || pick === null}
                    onClick={pick?.group === "recorded" ? onLink : onConfirm}
                  >
                    {primaryLabel}
                  </button>
                </>
              ) : null}
            </div>
          </div>
        )}
        {rowError && (
          <p role="alert" className="mt-3 text-sm text-[var(--state-danger)]">
            {rowError}
          </p>
        )}
      </div>
    </section>
  );
}

import type { ReconciliationInboxItem } from "@/lib/api/workspace/accounting.api";
import { CcAmount } from "./CcAmount";
import { ccD, ccHasDate, ccMon } from "./ccFormat";
import { SITUATION_CLASS, type Situation } from "./ReconciliationTabs";

const OPENED_ROW = "bg-[var(--cc-sel)]";

export function TransferList({
  situation,
  rows,
  openedId,
  onOpen,
}: {
  situation: Situation;
  rows: ReconciliationInboxItem[];
  openedId: number | null;
  onOpen: (id: number) => void;
}) {
  return (
    <ul
      aria-label="Transfers"
      className={`${SITUATION_CLASS[situation]} min-h-0 overflow-y-auto rounded-[14px] border border-[var(--cc-edge)] bg-[var(--cc-card)]`}
    >
      {rows.map((item) => {
        const opened = item.txn_id === openedId;
        const validDate = ccHasDate(item.txn_date);
        return (
          <li key={item.txn_id}>
            <button
              type="button"
              data-testid={`inbox-row-${item.txn_id}`}
              aria-current={opened ? "true" : undefined}
              onClick={() => onOpen(item.txn_id)}
              className={`relative grid min-h-[72px] w-full grid-cols-[40px_minmax(0,1fr)_auto] items-center gap-4 border-b border-[var(--cc-hair)] px-[22px] py-3 text-left text-[var(--cc-tx)] ${opened ? OPENED_ROW : ""}`}
            >
              {opened && (
                <span
                  aria-hidden="true"
                  data-testid="cc-marker"
                  className="absolute bottom-2.5 left-0 top-2.5 w-1 rounded-r bg-[var(--cc-mark)]"
                />
              )}
              <span className="flex flex-col items-start leading-none">
                {validDate ? (
                  <>
                    <b className="font-serif text-xl font-semibold leading-none">
                      {ccD(item.txn_date)}
                    </b>
                    <span className="mt-[3px] text-xs text-[var(--cc-tx-2)]">
                      {ccMon(item.txn_date)}
                    </span>
                  </>
                ) : (
                  <span className="text-xs text-[var(--cc-tx-2)]">
                    {item.txn_date}
                  </span>
                )}
              </span>
              <span className="line-clamp-2 text-sm font-medium [overflow-wrap:anywhere]">
                {item.description || "(no memo)"}
              </span>
              <span className="text-base font-medium">
                <CcAmount value={item.amount_idr} />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

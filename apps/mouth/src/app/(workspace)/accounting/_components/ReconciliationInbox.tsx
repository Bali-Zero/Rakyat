"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useToast } from "@/components/ui/toast";
import { logger } from "@/lib/logger";
import {
  accountingApi,
  type BankTransaction,
  type MatchCandidate,
  type MatchCandidatesResponse,
  type ReconciliationInboxItem,
} from "@/lib/api/workspace/accounting.api";
import { BankStatementUpload, BANK_FILE_INPUT_ID } from "./BankStatementUpload";
import { CompanyStrip, useCompanies } from "./CompanyStrip";
import { ConfirmModal } from "./ConfirmModal";
import { useDoneToday } from "./DoneTodaySection";
import { LinkedIgnoredSection } from "./LinkedIgnoredSection";
import { OpenedTransfer, type CandidatePick } from "./OpenedTransfer";
import {
  ReconciliationTabs,
  SITUATIONS,
  type ReconciliationTab,
  type Situation,
} from "./ReconciliationTabs";
import {
  ReconciliationBody,
  ReconciliationMore,
  type CentreView,
} from "./ReconciliationViews";
import { TransferList } from "./TransferList";
import { ccDayDate } from "./ccFormat";
import { useBankDate } from "./useBankDate";
import { useWriteLock } from "./useWriteLock";

type Light = "green" | "recorded" | "yellow" | "red";

const SITUATION_BY_LIGHT: Record<Light, Situation> = {
  green: "confirm",
  recorded: "link",
  yellow: "choose",
  red: "nomatch",
};

const normalizeLight = (light: string): Light =>
  light === "green" || light === "recorded" || light === "red"
    ? light
    : "yellow";

// The whole waiting list is loaded, not the newest page: counts per company must be complete.
const INBOX_LIMIT = 1000;

const MIN_REASON = 3;
const MAX_REASON = 200;
const normalizeReason = (raw: string): string =>
  raw.trim().replace(/\s+/g, " ");

// Historical mode box: the warning edge says the mode is on; off, it sits on the hairline.
const HISTORICAL_BOX =
  "flex cursor-pointer items-start gap-3 rounded-xl border p-3 text-sm text-[var(--cc-tx)]";
const HISTORICAL_BOX_ON = `${HISTORICAL_BOX} border-[var(--state-warning)] bg-[var(--cc-wash)]`;
const HISTORICAL_BOX_OFF = `${HISTORICAL_BOX} border-[var(--cc-edge)] bg-[var(--cc-card)]`;

export function ReconciliationInbox({
  onConfirmed,
  doneVersion,
  onOpenCashout,
}: {
  onConfirmed: () => void;
  doneVersion: number;
  onOpenCashout: () => void;
}) {
  const { error, success } = useToast();
  const [items, setItems] = useState<ReconciliationInboxItem[]>([]);
  const [loadStatus, setLoadStatus] = useState<"loading" | "ok" | "failed">(
    "loading",
  );
  const [selectedTab, setSelectedTab] = useState<ReconciliationTab | null>(
    null,
  );
  const [selectedTxn, setSelectedTxn] = useState<number | null>(null);
  const [candidates, setCandidates] = useState<MatchCandidatesResponse | null>(
    null,
  );
  const [candLoading, setCandLoading] = useState(false);
  const [candFailed, setCandFailed] = useState(false);
  const [chooseAnotherTxn, setChooseAnotherTxn] = useState<number | null>(null);
  const pickRef = useRef<CandidatePick | null>(null);
  const [pick, setPickState] = useState<CandidatePick | null>(null);
  const setPick = (value: CandidatePick | null) => {
    pickRef.current = value;
    setPickState(value);
  };
  const [modal, setModal] = useState<{
    txn: Pick<
      BankTransaction,
      "id" | "amount_idr" | "description" | "txn_date"
    >;
    candidate: MatchCandidate;
    // The mode at the moment the dialog opened: it cannot change under the dialog.
    historical: boolean;
  } | null>(null);
  // Historical mode: payments that arrived in the past are recorded without emails.
  const [historical, setHistorical] = useState(false);
  const ignoreRef = useRef<{ txnId: number; reason: string } | null>(null);
  const [ignoreForm, setIgnoreFormState] = useState<{
    txnId: number;
    reason: string;
  } | null>(null);
  const setIgnoreForm = (value: { txnId: number; reason: string } | null) => {
    ignoreRef.current = value;
    setIgnoreFormState(value);
  };
  const lock = useWriteLock();
  const [rowError, setRowError] = useState<{
    txnId: number;
    message: string;
  } | null>(null);
  const [version, setVersion] = useState(0);
  const loadReq = useRef(0);
  const candReq = useRef(0);
  const {
    data: done,
    status: doneStatus,
    unavailable,
    retry: retryDone,
  } = useDoneToday(doneVersion);
  const bankDate = useBankDate();
  const companies = useCompanies();
  // The company whose transfers the page shows; null shows every company.
  const [company, setCompany] = useState<string | null>(null);

  const load = useCallback(async () => {
    const token = ++loadReq.current;
    setLoadStatus("loading");
    try {
      const rows = await accountingApi.getReconciliationInbox(INBOX_LIMIT);
      if (loadReq.current !== token) return;
      setItems(rows);
      setLoadStatus("ok");
    } catch (err) {
      if (loadReq.current !== token) return;
      setLoadStatus("failed");
      error("Failed to load reconciliation inbox", "Please try again");
      logger.error("accounting inbox load failed", {}, err as Error);
    }
  }, [error]);

  useEffect(() => {
    void load();
  }, [load]);

  const afterWrite = (txnId: number) => {
    setItems((cur) => cur.filter((it) => it.txn_id !== txnId));
    if (ignoreRef.current?.txnId === txnId) setIgnoreForm(null);
    setVersion((v) => v + 1);
    void load();
    void companies.reload();
    onConfirmed();
  };

  const runAction = (
    txnId: number,
    title: string,
    run: () => Promise<unknown>,
  ) =>
    lock.runWrite(async () => {
      setRowError(null);
      try {
        await run();
        success(title);
        afterWrite(txnId);
      } catch (err) {
        const detail =
          (err instanceof Error && err.message) || "Please try again";
        setRowError({ txnId, message: detail });
        error("Action failed", detail);
        logger.error(
          `accounting ${title} failed txn=${txnId}`,
          {},
          err as Error,
        );
      }
    });

  const counts: Record<Situation, number> = {
    confirm: 0,
    link: 0,
    choose: 0,
    nomatch: 0,
  };
  const scoped =
    company === null
      ? items
      : items.filter((item) => item.company_key === company);
  for (const item of scoped) {
    counts[SITUATION_BY_LIGHT[normalizeLight(item.light)]] += 1;
  }

  let activeTab: ReconciliationTab | null = null;
  if (selectedTab === "done" && !unavailable) activeTab = "done";
  else if (selectedTab && selectedTab !== "done" && counts[selectedTab] > 0)
    activeTab = selectedTab;
  else
    activeTab = SITUATIONS.find((situation) => counts[situation] > 0) ?? null;

  const activeSituation = activeTab !== "done" ? activeTab : null;
  const rows = activeSituation
    ? scoped.filter(
        (item) =>
          SITUATION_BY_LIGHT[normalizeLight(item.light)] === activeSituation,
      )
    : [];
  const opened = activeSituation
    ? (rows.find((item) => item.txn_id === selectedTxn) ?? rows[0] ?? null)
    : null;
  const openedId = opened?.txn_id ?? null;

  const loadCandidates = useCallback(
    async (item: ReconciliationInboxItem) => {
      const token = ++candReq.current;
      setPick(null);
      setChooseAnotherTxn(null);
      setCandidates(null);
      setCandFailed(false);
      setCandLoading(true);
      try {
        const response = await accountingApi.getMatchCandidates(item.txn_id);
        if (candReq.current !== token || response.txn_id !== item.txn_id)
          return;
        setCandidates(response);
        const light = normalizeLight(item.light);
        const defaultCandidate =
          light === "green"
            ? response.candidates.find(
                (candidate) =>
                  candidate.invoice_id === item.suggestion?.invoice_id,
              )
            : light === "recorded"
              ? response.recorded_candidates.find(
                  (candidate) =>
                    candidate.invoice_id === item.suggestion?.invoice_id,
                )
              : undefined;
        setPick(
          defaultCandidate
            ? {
                txnId: item.txn_id,
                group: light === "recorded" ? "recorded" : "open",
                candidate: defaultCandidate,
              }
            : null,
        );
      } catch (err) {
        if (candReq.current !== token) return;
        setCandFailed(true);
        error("Failed to load candidates", "Please try again");
        logger.error(
          `accounting candidates failed txn=${item.txn_id}`,
          {},
          err as Error,
        );
      } finally {
        if (candReq.current === token) setCandLoading(false);
      }
    },
    [error],
  );

  useEffect(() => {
    if (opened) void loadCandidates(opened);
    else {
      candReq.current += 1;
      setPick(null);
      setCandidates(null);
      setCandLoading(false);
      setCandFailed(false);
    }
  }, [openedId, loadCandidates]);

  const openIgnore = (txnId: number) => {
    if (!lock.isFree()) return;
    if (ignoreRef.current?.txnId === txnId) return;
    setRowError(null);
    setIgnoreForm({ txnId, reason: "" });
  };
  const closeIgnore = (txnId: number) => {
    if (!lock.isFree()) return;
    if (ignoreRef.current?.txnId !== txnId) return;
    setIgnoreForm(null);
  };
  const setIgnoreReason = (txnId: number, text: string) => {
    const current = ignoreRef.current;
    if (!current || current.txnId !== txnId) return;
    setIgnoreForm({ ...current, reason: text });
  };
  const sendIgnore = (txnId: number) => {
    const current = ignoreRef.current;
    if (!current || current.txnId !== txnId) return;
    const text = normalizeReason(current.reason);
    const length = Array.from(text).length;
    if (length < MIN_REASON || length > MAX_REASON) return;
    void runAction(txnId, "Transfer ignored", () =>
      accountingApi.ignoreTransaction({ bank_txn_id: txnId, reason: text }),
    );
  };

  const confirm = () => {
    const current = pickRef.current;
    if (
      !opened ||
      !current ||
      current.txnId !== opened.txn_id ||
      current.group !== "open"
    )
      return;
    const txn = {
      id: opened.txn_id,
      amount_idr: opened.amount_idr,
      description: opened.description,
      txn_date: opened.txn_date,
    };
    if (lock.tryOpenDialog())
      setModal({ txn, candidate: current.candidate, historical });
  };
  const link = () => {
    const current = pickRef.current;
    if (
      !opened ||
      !current ||
      current.txnId !== opened.txn_id ||
      current.group !== "recorded"
    )
      return;
    void runAction(opened.txn_id, "Transfer linked", () =>
      accountingApi.linkRecorded({
        bank_txn_id: opened.txn_id,
        practice_id: current.candidate.practice_id,
        invoice_id: current.candidate.invoice_id,
      }),
    );
  };

  const reasonLength = Array.from(
    normalizeReason(ignoreForm?.reason ?? ""),
  ).length;
  const reasonOk = reasonLength >= MIN_REASON && reasonLength <= MAX_REASON;
  const defaultPick =
    opened && candidates
      ? normalizeLight(opened.light) === "green"
        ? candidates.candidates.find(
            (candidate) =>
              candidate.invoice_id === opened.suggestion?.invoice_id,
          )
        : normalizeLight(opened.light) === "recorded"
          ? candidates.recorded_candidates.find(
              (candidate) =>
                candidate.invoice_id === opened.suggestion?.invoice_id,
            )
          : undefined
      : undefined;
  const pair =
    opened !== null &&
    (activeSituation === "confirm" || activeSituation === "link") &&
    chooseAnotherTxn !== opened.txn_id &&
    defaultPick !== undefined &&
    pick?.candidate.invoice_id === defaultPick.invoice_id &&
    pick.txnId === opened.txn_id;
  const nothing =
    activeSituation === "nomatch" &&
    candidates !== null &&
    candidates.candidates.length === 0 &&
    candidates.recorded_candidates.length === 0 &&
    (opened?.open_candidates ?? 0) <= 0 &&
    (opened?.recorded_candidates ?? 0) <= 0;

  let view: CentreView;
  if (activeTab === "done") view = "done";
  else if (items.length === 0 && loadStatus === "loading") view = "loading";
  else if (items.length === 0 && loadStatus === "failed") view = "failed";
  else if (
    items.length === 0 &&
    loadStatus === "ok" &&
    bankDate.status === "empty"
  )
    view = "dayone";
  else if (scoped.length === 0) view = "calm";
  else view = "work";

  const selectedCompany =
    company === null
      ? null
      : (companies.rows.find((row) => row.key === company) ?? null);
  const status = selectedCompany ? (
    <p data-testid="cc-bank-date" className="text-sm text-[var(--cc-tx-2)]">
      {selectedCompany.data_through ? (
        <>
          {selectedCompany.company_name}: bank data through{" "}
          <b className="font-bold text-[var(--cc-tx)]">
            {ccDayDate(selectedCompany.data_through)}
          </b>
        </>
      ) : (
        `${selectedCompany.company_name}: no statement yet`
      )}
    </p>
  ) : bankDate.status === "loading" ? null : (
    <p data-testid="cc-bank-date" className="text-sm text-[var(--cc-tx-2)]">
      {bankDate.status === "ok" ? (
        <>
          Bank data through{" "}
          <b className="font-bold text-[var(--cc-tx)]">
            {ccDayDate(bankDate.date)}
          </b>
        </>
      ) : bankDate.status === "empty" ? (
        "No bank data yet"
      ) : (
        "Bank data date could not be loaded."
      )}
    </p>
  );

  const linkedIgnored = (
    <LinkedIgnoredSection
      version={version}
      locked={lock.locked}
      exclusive={lock.runWrite}
      onReopened={() => {
        void load();
        void companies.reload();
        onConfirmed();
      }}
    />
  );
  const work =
    activeSituation && opened ? (
      <div
        data-testid="cc-work"
        className="mt-5 grid h-[550px] grid-cols-[344px_minmax(0,1fr)] grid-rows-[minmax(0,1fr)] gap-6"
      >
        <TransferList
          situation={activeSituation}
          rows={rows}
          openedId={opened.txn_id}
          onOpen={(txnId) => {
            // A dialog or a pending write acts on the opened transfer: it stays opened.
            if (txnId !== opened.txn_id && lock.isFree()) setSelectedTxn(txnId);
          }}
        />
        <OpenedTransfer
          situation={activeSituation}
          item={opened}
          rowsCount={rows.length}
          candidates={candidates}
          loading={candLoading}
          failed={candFailed}
          pair={pair}
          nothing={nothing}
          pick={pick}
          ignoreForm={ignoreForm}
          reasonOk={reasonOk}
          locked={lock.locked}
          rowError={rowError?.txnId === opened.txn_id ? rowError.message : null}
          onRetry={() => void loadCandidates(opened)}
          onChooseAnother={() => setChooseAnotherTxn(opened.txn_id)}
          onPick={setPick}
          onConfirm={confirm}
          onLink={link}
          onSkip={() => {
            const index = rows.findIndex((row) => row.txn_id === opened.txn_id);
            setSelectedTxn(rows[(index + 1) % rows.length].txn_id);
          }}
          onIgnore={() => openIgnore(opened.txn_id)}
          onIgnoreReason={(text) => setIgnoreReason(opened.txn_id, text)}
          onSendIgnore={() => sendIgnore(opened.txn_id)}
          onCancelIgnore={() => closeIgnore(opened.txn_id)}
        />
      </div>
    ) : null;

  return (
    <div className="cc-root flex flex-col">
      <BankStatementUpload
        title="Accounting"
        status={status}
        hideTrigger={view === "dayone"}
        locked={lock.locked}
        exclusive={lock.runWrite}
        onImported={() => {
          void load();
          void bankDate.reload();
          void companies.reload();
          onConfirmed();
        }}
      />
      <CompanyStrip
        companies={companies}
        selected={company}
        locked={lock.locked}
        onSelect={(key) => {
          if (lock.isFree()) setCompany(key);
        }}
      />
      {view !== "dayone" && (
        <ReconciliationTabs
          active={activeTab}
          counts={counts}
          loaded={items.length > 0 || loadStatus === "ok"}
          done={done}
          doneUnavailable={unavailable}
          onSelect={setSelectedTab}
        />
      )}
      {view !== "dayone" && (
        <label
          data-testid="historical-mode"
          className={historical ? HISTORICAL_BOX_ON : HISTORICAL_BOX_OFF}
        >
          <input
            type="checkbox"
            className="mt-0.5"
            checked={historical}
            disabled={lock.locked}
            onChange={(e) => setHistorical(e.target.checked)}
          />
          <span>
            <span className="font-medium">Historical mode: no emails</span>
            <span className="block text-xs text-[var(--cc-tx-2)]">
              For payments that arrived in the past. Confirming records them as
              usual and sends no email to the client or the team.
            </span>
          </span>
        </label>
      )}
      <ReconciliationBody
        view={view}
        work={work}
        bankDate={bankDate}
        done={done}
        doneStatus={doneStatus}
        doneUnavailable={unavailable}
        linkedIgnored={linkedIgnored}
        onRetryInbox={() => void load()}
        onRetryDone={() => void retryDone()}
        onShowDone={() => setSelectedTab("done")}
        onUpload={() => document.getElementById(BANK_FILE_INPUT_ID)?.click()}
      />
      <ReconciliationMore onOpenCashout={onOpenCashout} />
      {modal && (
        <ConfirmModal
          txn={modal.txn}
          candidate={modal.candidate}
          historical={modal.historical}
          onClose={() => {
            if (lock.tryCloseDialog()) setModal(null);
          }}
          exclusive={lock.runDialogWrite}
          onConfirmed={() => {
            lock.dialogDone();
            setModal(null);
            afterWrite(modal.txn.id);
          }}
        />
      )}
    </div>
  );
}

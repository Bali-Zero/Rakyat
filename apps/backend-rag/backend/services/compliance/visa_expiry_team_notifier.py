"""
Visa Expiry Team Notifier

Alerts team leaders (assigned_to) when their clients' visa, KITAS,
or passport documents are expiring in 60, 30, or 7 days.
Sends individual emails per team leader + summary to zero@.
"""

from __future__ import annotations

import os
from collections import defaultdict
from html import escape
from typing import Any

import asyncpg
import httpx

from backend.app.utils.logging_utils import get_logger
from backend.services.notifications.email_audit import raise_if_not_delivered

logger = get_logger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
_EMAIL_API_URL: str = os.getenv(
    "INTERNAL_EMAIL_API_URL",
    "https://nuzantara-rag.fly.dev/api/notifications/send-email",
)
_EMAIL_API_KEY: str = os.getenv("NUZANTARA_API_KEY", "")

ADMIN_EMAIL: str = "zero@balizero.com"
SENDER_EMAIL: str = "zantara@balizero.com"
SENDER_NAME: str = "Zantara"

ALERT_THRESHOLDS: tuple[int, ...] = (7, 30, 60)

# ── Colour coding per soglia (HTML) ────────────────────────────────────────────
_ROW_COLOURS: dict[int, str] = {
    7: "#FFCCCC",  # red
    30: "#FFE0B2",  # orange
    60: "#FFFDE7",  # yellow
}
_LABEL_COLOURS: dict[int, str] = {
    7: "#C62828",
    30: "#E65100",
    60: "#F9A825",
}

# ── SQL per ogni tipo di documento ─────────────────────────────────────────────
_DOC_QUERIES: list[tuple[str, str]] = [
    ("visa", "visa_expiry_date"),
    ("kitas", "kitas_expiry_date"),
    ("passport", "passport_expiry"),
]

_BASE_SQL = """
SELECT
    c.id          AS client_id,
    c.full_name   AS client_name,
    c.email       AS client_email,
    c.phone       AS client_phone,
    c.nationality,
    c.assigned_to,
    $1::text      AS document_type,
    c.{col}       AS expiry_date,
    (c.{col} - CURRENT_DATE)::int AS days_until_expiry,
    c.current_visa_type,
    c.current_visa_sponsor,
    c.passport_expiry
FROM clients c
WHERE c.{col} IS NOT NULL
  AND c.{col} BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '60 days'
  AND c.assigned_to IS NOT NULL
  AND c.deleted_at IS NULL
"""


class _ExpiryRead(list[dict[str, Any]]):
    """The rows of one read, and the document types that read could not cover.

    It is a list, so everything that takes the rows keeps taking them. The unread
    types travel with the rows of the read that found them and are never kept on
    the notifier, where a second run would overwrite them under a first one.
    """

    unread: tuple[str, ...] = ()


class VisaExpiryTeamNotifier:
    """
    Checks the database for clients whose documents expire in 7, 30, or 60 days
    and sends proactive alert emails to the responsible team leader.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._db_pool = db_pool
        self._client: httpx.AsyncClient | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    async def check_and_notify(self) -> dict[str, Any]:
        """
        Find clients with visa/kitas/passport expiring in 7, 30, or 60 days.
        Group by team leader (assigned_to).
        Send one email per team leader + a summary to zero@.
        Returns a dict with ``total_alerts``, ``emails_sent``, and ``emails_failed`` counts.
        """
        logger.info("VisaExpiryTeamNotifier: starting check")

        expiring: list[dict[str, Any]] = await self._get_expiring_documents()
        # Plain rows, from a caller that supplies its own, name nothing as unread.
        unread: tuple[str, ...] = expiring.unread if isinstance(expiring, _ExpiryRead) else ()
        if not expiring and not unread:
            logger.info("VisaExpiryTeamNotifier: no expiring documents found")
            return {"total_alerts": 0, "emails_sent": 0, "emails_failed": 0}
        if unread:
            # A run that could not read a document type is not a run that found
            # nothing of that type: it is said in the result and in every mail,
            # and the summary goes out even when nothing else is expiring.
            logger.error(
                "VisaExpiryTeamNotifier: expiry dates not read for %s — this run is incomplete",
                ", ".join(unread),
            )

        logger.info(
            "VisaExpiryTeamNotifier: found %d expiring document(s) across %d client(s)",
            len(expiring),
            len({r["client_id"] for r in expiring}),
        )

        # Group by team leader
        by_leader: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in expiring:
            by_leader[row["assigned_to"]].append(row)

        # Send per-leader alerts
        emails_sent = 0
        emails_failed = 0
        async with self._get_http_client() as client:
            self._client = client
            for team_email, clients in by_leader.items():
                try:
                    await self._send_team_leader_alert(team_email, clients, unread)
                    emails_sent += 1
                except Exception:
                    emails_failed += 1
                    logger.exception("Failed to send alert to team leader %s", team_email)

            # Send summary to admin
            try:
                await self._send_zero_summary(expiring, unread)
                emails_sent += 1
            except Exception:
                emails_failed += 1
                logger.exception("Failed to send summary email to %s", ADMIN_EMAIL)

        logger.info(
            "VisaExpiryTeamNotifier: done — %d emails sent, %d failed",
            emails_sent,
            emails_failed,
        )
        result: dict[str, Any] = {
            "total_alerts": len(expiring),
            "emails_sent": emails_sent,
            "emails_failed": emails_failed,
        }
        if unread:
            result["failed_queries"] = list(unread)
        return result

    # ── Database query ─────────────────────────────────────────────────────────

    async def _get_expiring_documents(self) -> _ExpiryRead:
        """
        Returns rows for clients where any of visa_expiry_date, kitas_expiry_date,
        or passport_expiry_date falls within the next 60 days.

        Each row contains:
            client_id, client_name, client_email, client_phone, nationality,
            assigned_to, document_type, expiry_date, days_until_expiry
        Sorted by days_until_expiry ASC.

        A document type whose query fails, a missing column included, does not
        stop the others; it is recorded on the returned read as ``unread``, so that
        the run can say what its list does not cover.
        """
        rows = _ExpiryRead()
        unread: list[str] = []

        async with self._db_pool.acquire() as conn:
            for doc_type, col in _DOC_QUERIES:
                sql = _BASE_SQL.format(col=col)
                try:
                    result = await conn.fetch(sql, doc_type)
                    rows.extend(dict(r) for r in result)
                except asyncpg.UndefinedColumnError:
                    unread.append(doc_type)
                    logger.warning(
                        "Column '%s' not found in 'clients' table — skipping %s alerts",
                        col,
                        doc_type,
                    )
                except Exception:
                    unread.append(doc_type)
                    logger.exception("Unexpected error querying '%s' expiry dates", doc_type)

        rows.unread = tuple(unread)

        # Sort combined results by days_until_expiry ASC
        rows.sort(key=lambda r: r.get("days_until_expiry", 9999))
        return rows

    # ── Email helpers ──────────────────────────────────────────────────────────

    async def _send_team_leader_alert(
        self,
        team_email: str,
        clients: list[dict[str, Any]],
        unread: tuple[str, ...] = (),
    ) -> None:
        """
        Sends an HTML email to the team leader listing their clients whose
        documents expire soon.  Rows are colour-coded by urgency.
        Subject: "⚠️ Documenti in scadenza — {n} clienti da contattare"
        """
        n = len(clients)
        subject = (
            f"[EXPIRY] ⚠️ Documents expiring — {n} client{'s' if n != 1 else ''} to contact"
        )
        if unread:
            subject += " (incomplete list)"

        rows_html = "\n".join(_build_client_row(c) for c in clients)

        body = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333; line-height: 1.5;">

<h2 style="color: #c62828;">⚠️ Documents expiring — action required</h2>{_incomplete_notice(unread)}

<p>Hello,<br>
The following clients of yours have documents expiring in the next 60 days.
Please contact them as soon as possible to start the renewal process with Bali Zero.</p>

<table border="1" cellpadding="8" cellspacing="0"
       style="border-collapse:collapse; width:100%; font-size:14px;">
  <thead style="background:#1a237e; color:#fff;">
    <tr>
      <th>Client</th>
      <th>Document</th>
      <th>Expiry</th>
      <th>Days left</th>
      <th>Phone</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>

<br>
<p style="font-size:13px; color:#555;">
  <strong>Colour legend:</strong>
  <span style="background:{_ROW_COLOURS[7]}; padding:2px 8px; border-radius:4px;">
    ≤ 7 days
  </span>
  &nbsp;
  <span style="background:{_ROW_COLOURS[30]}; padding:2px 8px; border-radius:4px;">
    ≤ 30 days
  </span>
  &nbsp;
  <span style="background:{_ROW_COLOURS[60]}; padding:2px 8px; border-radius:4px;">
    ≤ 60 days
  </span>
</p>

<p style="font-size:13px; color:#555;">
  To start the renewal, contact the client directly or open a new practice
  at <a href="https://kita.balizero.com">kita.balizero.com</a>.
</p>

<p>— Zantara · Bali Zero</p>
</body>
</html>
""".strip()

        await self._post_email(to=team_email, subject=subject, html_body=body)
        logger.info("Team leader alert sent to %s (%d client(s))", team_email, n)

    async def _send_zero_summary(
        self,
        all_clients: list[dict[str, Any]],
        unread: tuple[str, ...] = (),
    ) -> None:
        """
        Summary email to zero@ with all expiring documents grouped by team leader.
        Subject: "⚠️ {n} documenti in scadenza — riepilogo team"
        """
        n = len(all_clients)
        subject = f"[EXPIRY] ⚠️ {n} document{'s' if n != 1 else ''} expiring — team summary"
        if unread:
            subject += " (incomplete)"

        # Group by leader for the summary table
        by_leader: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in all_clients:
            by_leader[row["assigned_to"]].append(row)

        sections: list[str] = []
        for leader_email, clients in sorted(by_leader.items()):
            rows_html = "\n".join(_build_client_row(c) for c in clients)
            section = f"""
<h3 style="color:#1a237e; margin-top:24px;">
  👤 {escape(leader_email)} — {len(clients)} client{"s" if len(clients) != 1 else ""}
</h3>
<table border="1" cellpadding="7" cellspacing="0"
       style="border-collapse:collapse; width:100%; font-size:13px;">
  <thead style="background:#37474f; color:#fff;">
    <tr>
      <th>Client</th>
      <th>Document</th>
      <th>Expiry</th>
      <th>Days left</th>
      <th>Phone</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
"""
            sections.append(section)

        body = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333; line-height: 1.5;">

<h2 style="color: #c62828;">⚠️ Expiring documents summary — {n} total</h2>{_incomplete_notice(unread)}

<p>This summary is sent automatically by Zantara.<br>
Each team leader has already received their individual notification.</p>

{"".join(sections)}

<br>
<p style="font-size:12px; color:#888;">
  Generated automatically by Zantara · Bali Zero —
  <a href="https://kita.balizero.com">kita.balizero.com</a>
</p>
</body>
</html>
""".strip()

        await self._post_email(to=ADMIN_EMAIL, subject=subject, html_body=body)
        logger.info(
            "Summary email sent to %s (%d document(s), %d leader(s))",
            ADMIN_EMAIL,
            n,
            len(by_leader),
        )

    # ── Internal HTTP helper ───────────────────────────────────────────────────

    async def _post_email(
        self,
        to: str,
        subject: str,
        html_body: str,
    ) -> None:
        """
        POSTs an email via the internal Brevo endpoint.
        Raises httpx.HTTPStatusError on non-2xx responses, and EmailNotDelivered when the
        endpoint answers 200 and its body says the mail was not sent.
        """
        if self._client is None:
            raise RuntimeError("_post_email called outside of an active HTTP client context")

        payload: dict[str, Any] = {
            "to": to,
            "subject": subject,
            "body": html_body,
        }

        response = await self._client.post(
            _EMAIL_API_URL,
            json=payload,
            headers={
                "X-API-Key": _EMAIL_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )
        response.raise_for_status()
        raise_if_not_delivered(response)
        logger.debug("Email delivered to %s (status %d)", to, response.status_code)

    @staticmethod
    def _get_http_client() -> httpx.AsyncClient:
        """Creates a reusable AsyncClient for a single notification run.

        Caller wraps in ``async with self._get_http_client() as client:``
        — deterministic close. Equivalent to OK_CONTEXT_MANAGER per the
        P0-5 audit.
        """
        return (
            httpx.AsyncClient(  # golden-rule-10-exempt: factory used exclusively via `async with`
                headers={"User-Agent": "VisaExpiryTeamNotifier/1.0"},
            )
        )


# ── Private rendering helpers ──────────────────────────────────────────────────


def _incomplete_notice(unread: tuple[str, ...]) -> str:
    """The warning a mail carries when its list was built without some document types.

    It brings its own line break: a mail with nothing unread is, byte for byte,
    the mail it was before the notice existed.
    """
    if not unread:
        return ""
    names = ", ".join(escape(doc_type.upper()) for doc_type in unread)
    return (
        '\n<p style="background:#FFEBEE; border:1px solid #C62828; padding:10px;">'
        "<strong>This list is incomplete.</strong> "
        f"The expiry dates of these documents could not be read in this run: {names}. "
        "A client whose only expiring document is one of them is missing from it."
        "</p>"
    )


def _build_client_row(client: dict[str, Any]) -> str:
    """Returns an HTML <tr> for one expiring document, colour-coded by urgency."""
    days: int = int(client.get("days_until_expiry") or 0)
    days_html = escape(str(days))
    expiry_date: str = escape(_fmt_date(client.get("expiry_date")))
    doc_type: str = escape(str(client.get("document_type", "")).upper())
    name: str = escape(str(client.get("client_name") or "—"))
    phone: str = escape(str(client.get("client_phone") or "—"))

    # Pick the closest threshold colour (7 ≤ 30 ≤ 60)
    if days <= 7:
        bg = _ROW_COLOURS[7]
        label_color = _LABEL_COLOURS[7]
    elif days <= 30:
        bg = _ROW_COLOURS[30]
        label_color = _LABEL_COLOURS[30]
    else:
        bg = _ROW_COLOURS[60]
        label_color = _LABEL_COLOURS[60]

    # Enhanced: show visa type if available
    visa_type = client.get("current_visa_type")
    if visa_type and doc_type in ("VISA", "KITAS"):
        doc_type = f"{doc_type} ({escape(str(visa_type))})"

    sponsor = client.get("current_visa_sponsor", "")
    sponsor_html = (
        f"<br><small style='color:#666;'>Sponsor: {escape(str(sponsor))}</small>" if sponsor else ""
    )

    # Passport cross-check warning
    passport_warning = ""
    passport_expiry = client.get("passport_expiry")
    expiry = client.get("expiry_date")
    if passport_expiry and expiry and passport_expiry < expiry:
        passport_warning = (
            f"<br><small style='color:#C62828;'>⚠️ Passaporto scade PRIMA "
            f"({escape(passport_expiry.strftime('%d/%m/%Y'))})</small>"
        )

    return (
        f'<tr style="background:{bg};">'
        f"<td style='padding:8px;'>{name}</td>"
        f"<td style='padding:8px;'>{doc_type}{sponsor_html}</td>"
        f"<td style='padding:8px;'>{expiry_date}{passport_warning}</td>"
        f"<td style='padding:8px; font-weight:bold; color:{label_color};'>{days_html} giorni</td>"
        f"<td style='padding:8px;'>{phone}</td>"
        f"</tr>"
    )


def _fmt_date(value: Any) -> str:
    """Formats a date value to DD/MM/YYYY string, or '—' if None."""
    if value is None:
        return "—"
    try:
        # asyncpg returns datetime.date objects directly
        return value.strftime("%d/%m/%Y")
    except AttributeError:
        return str(value)

"""
AlertsEngine — single entrypoint for compliance alert generation (decision #1).

Responsibilities:
- Orchestrate Predictive → Dedup → Repository → Dispatcher
- Render i18n templates (IT/EN/ID)
- Populate estimated_cost_idr from PricingTool (never hardcoded)
- Handle severity promotion on re-scan

Uses:
- AlertRepository (m114)
- AlertDedup (build_dedup_key, should_promote)
- templates_i18n.render_template
- PricingTool.get_price (lookup-based, None allowed)
- AlertDispatcher.dispatch (async, called after insert/promote)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
import jinja2

from backend.services.compliance.alert_dedup import build_dedup_key, should_promote
from backend.services.compliance.alert_repository import AlertRepository, AlertRow
from backend.services.compliance.predictive_engine import ComplianceForecast
from backend.services.compliance.severity_calculator import AlertSeverity
from backend.services.compliance.templates_i18n import render_template

logger = logging.getLogger(__name__)


_DOCTYPE_TO_CATEGORY: dict[str, str] = {
    "visa": "visa_expiry",
    "kitas": "visa_expiry",
    "passport": "document_expiry",
    "license": "license_renewal",
    # E33 Second Home lifecycle (services/crm/e33_lifecycle.py) — the Day-90
    # guarantee gate and the annual maintenance recurrence.
    "e33_guarantee": "guarantee_proof",
    "e33_maintenance": "guarantee_proof",
}

# Categories whose dedup key stands for a document date of the client row (the visa and KITAS
# dates share the visa key, the passport has its own). A later date under such a key means the
# document was renewed, so an open alert whose deadline has passed gives way to it. License and
# E33 guarantee keys gather several obligations of one client, and a passed one may still be
# outstanding: those keep their alert.
_ONE_DOCUMENT_CATEGORIES: frozenset[str] = frozenset({"visa_expiry", "document_expiry"})


def _urgency_to_severity(urgency: str) -> str:
    # PredictiveEngine.urgency_level strings: "info" | "warning" | "urgent" | "critical"
    if urgency in {"info", "warning", "urgent", "critical"}:
        return urgency
    return "info"


def _reporting_period(forecast: ComplianceForecast) -> str | None:
    # Only lkpm / tax_filing carry a period — synthesize from expiry.
    if forecast.document_type in {"lkpm", "tax"}:
        y = forecast.expiry_date.year
        q = ((forecast.expiry_date.month - 1) // 3) + 1
        return f"{y}-Q{q}"
    return None


class AlertsEngine:
    """
    Orchestrator. Never holds state across generate_alerts calls.
    """

    def __init__(
        self,
        db_pool: asyncpg.Pool,
        *,
        pricing: Any,
        dispatcher: Any,
        connection: asyncpg.Connection | None = None,
    ) -> None:
        if connection is not None and db_pool is not None:
            raise ValueError("pass either db_pool or connection, not both")
        self._pool = db_pool
        self._pricing = pricing
        self._dispatcher = dispatcher
        self._conn = connection
        self._repo = (
            AlertRepository.with_connection(connection)
            if connection is not None
            else AlertRepository(db_pool)
        )

    @classmethod
    def with_connection(
        cls,
        conn: asyncpg.Connection,
        *,
        pricing: Any,
        dispatcher: Any,
    ) -> AlertsEngine:
        inst = cls.__new__(cls)
        inst._pool = None  # type: ignore[assignment]
        inst._conn = conn
        inst._pricing = pricing
        inst._dispatcher = dispatcher
        inst._repo = AlertRepository.with_connection(conn)
        return inst

    async def generate_alerts(
        self,
        forecasts: list[ComplianceForecast],
        *,
        client_lang_resolver: Any = None,
    ) -> list[AlertRow]:
        """
        Build/promote alerts from a batch of forecasts.

        Returns a list mirroring `forecasts` by order (skipped dedup returns existing).
        Dispatcher failures do not abort generation.
        """
        if not forecasts:
            return []

        out: list[AlertRow] = []
        for fc in forecasts:
            try:
                alert = await self._handle_one(fc, client_lang_resolver)
            except (asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
                logger.error(
                    "DB error generating alert for client %s: %s (skipping this forecast)",
                    fc.client_id,
                    exc,
                )
                continue  # resilience: one bad forecast doesn't kill the batch
            if alert is not None:
                out.append(alert)
        return out

    async def _handle_one(
        self,
        fc: ComplianceForecast,
        client_lang_resolver: Any,
    ) -> AlertRow | None:
        category = _DOCTYPE_TO_CATEGORY.get(fc.document_type, fc.document_type)
        compliance_item_ref = fc.matched_rule_id
        reporting_period = _reporting_period(fc)

        try:
            dedup_key = build_dedup_key(
                category=category,
                client_id=fc.client_id,
                compliance_item_ref=compliance_item_ref,
                reporting_period=reporting_period,
            )
        except ValueError as exc:
            logger.warning("cannot dedup forecast %s: %s", fc.client_id, exc)
            return None

        existing = await self._repo.find_active_by_dedup_key(dedup_key)
        new_severity_str = _urgency_to_severity(fc.urgency_level)
        # Language ENFORCED ENGLISH-ONLY 2026-05-26 per operator decree (was IT default).
        # client_lang_resolver kept for backward compat but ignored for email dispatch.
        lang: str = "en"
        _ = client_lang_resolver  # silence unused warning

        if (
            existing is not None
            and category in _ONE_DOCUMENT_CATEGORIES
            and fc.expiry_date > existing.deadline
        ):
            # The stored alert may be about a deadline that has passed. The new alert is
            # rendered first, so a failed render leaves the stored one open; the repository
            # then closes the stored alert and inserts this one together, or changes nothing.
            row = self._new_row(fc, category, dedup_key, reporting_period, new_severity_str, lang)
            if row is not None:
                try:
                    replaced = await self._repo.replace_passed(existing.alert_id, row)
                except asyncpg.UniqueViolationError:
                    return await self._repo.find_active_by_dedup_key(dedup_key)
                if replaced is not None:
                    await self._safe_dispatch(replaced)
                    return replaced
                # Nothing was replaced: the stored alert is not passed, or another run has
                # replaced or closed it since this run read it. The key is read once more. A
                # run writes to, and sends for, only the alert it read, or a new one when the
                # key has none: an alert another run wrote is returned as found.
                current = await self._repo.find_active_by_dedup_key(dedup_key)
                if current is not None and current.alert_id != existing.alert_id:
                    return current
                existing = current

        if existing is not None:
            old_sev = AlertSeverity(existing.severity)
            new_sev = AlertSeverity(new_severity_str)
            if should_promote(old_sev, new_sev):
                # A promoted alert is dispatched, so it carries this forecast's deadline, texts
                # and cost; a failed render (logged) or an unknown cost keeps the stored value.
                texts = self._render(fc, category, reporting_period, lang) or {}
                promoted = await self._repo.promote(
                    existing.alert_id,
                    new_severity=new_severity_str,
                    new_days_until=fc.days_until_expiry,
                    deadline=fc.expiry_date,
                    estimated_cost_idr=self._cost(fc),
                    **texts,
                )
                if promoted is None:
                    # Refused by the statement: another run raised it as high or higher, or
                    # closed it, after this one read it. Nothing is sent.
                    return await self._repo.find_active_by_dedup_key(dedup_key)
                await self._safe_dispatch(promoted)
                return promoted
            # Same/lower severity → return existing, no dispatch.
            return existing

        row = self._new_row(fc, category, dedup_key, reporting_period, new_severity_str, lang)
        if row is None:
            return None

        try:
            inserted = await self._repo.insert(row)
        except asyncpg.UniqueViolationError:
            # Race: someone else inserted the same dedup_key. Re-query and return.
            existing = await self._repo.find_active_by_dedup_key(dedup_key)
            return existing
        await self._safe_dispatch(inserted)
        return inserted

    def _render(
        self,
        fc: ComplianceForecast,
        category: str,
        reporting_period: str | None,
        lang: str,
    ) -> dict[str, str] | None:
        """The alert texts of this forecast, or None (logged) when a template fails."""
        # Render messages in all three langs (column-per-lang snapshot).
        render_kwargs: dict[str, Any] = {
            "days_until": fc.days_until_expiry,
            "visa_type": fc.current_visa_type or "",
            "period": reporting_period or "",
            "title": category.replace("_", " ").title(),
            "license_type": fc.document_type,
            "permit_type": fc.document_type,
            "doc_type": fc.document_type,
            "topic": category,
        }
        try:
            message_it = render_template(category, "body", "it", **render_kwargs)
            message_en = render_template(category, "body", "en", **render_kwargs)
            message_id = render_template(category, "body", "id", **render_kwargs)
            action = render_template(category, "action", lang, **render_kwargs)
        except (jinja2.UndefinedError, KeyError) as exc:
            logger.error(
                "template render failed for client=%s category=%s: %s",
                fc.client_id,
                category,
                exc,
            )
            return None
        return {
            "message_it": message_it,
            "message_en": message_en,
            "message_id": message_id,
            "suggested_action": action,
        }

    def _cost(self, fc: ComplianceForecast) -> int | None:
        # Pricing — PricingTool only, NEVER hardcoded.
        cost: int | None = None
        if fc.renewal_pricing_key and self._pricing is not None:
            try:
                cost = self._pricing.get_price(fc.renewal_pricing_key)
            except Exception as exc:
                logger.warning("pricing lookup failed for %s: %s", fc.renewal_pricing_key, exc)
        return cost

    def _new_row(
        self,
        fc: ComplianceForecast,
        category: str,
        dedup_key: str,
        reporting_period: str | None,
        severity: str,
        lang: str,
    ) -> AlertRow | None:
        """A new pending alert for this forecast, or None when its texts cannot be rendered."""
        texts = self._render(fc, category, reporting_period, lang)
        if texts is None:
            return None
        return AlertRow(
            alert_id=f"alert_{category}_{fc.client_id}_{uuid.uuid4().hex[:8]}",
            client_id=fc.client_id,
            category=category,
            severity=severity,
            status="pending",
            deadline=fc.expiry_date,
            days_until=fc.days_until_expiry,
            compliance_item_ref=fc.matched_rule_id,
            dedup_key=dedup_key,
            estimated_cost_idr=self._cost(fc),
            evidence_refs=[],
            # NB-2 ref (if rule carries one)
            nb2_ref=self._lookup_nb2_ref(fc.matched_rule_id),
            **texts,
        )

    async def _safe_dispatch(self, alert: AlertRow) -> None:
        if self._dispatcher is None:
            return
        try:
            await self._dispatcher.dispatch(alert)
        except Exception as exc:
            logger.warning("dispatcher failed for %s: %s", alert.alert_id, exc)

    def _lookup_nb2_ref(self, rule_id: str | None) -> str | None:
        """Fetch nb2_ref from the renewal rule registry (decision #9)."""
        if not rule_id:
            return None
        try:
            from backend.services.compliance.renewal_rules import RENEWAL_RULES
        except ImportError:
            return None
        rule = RENEWAL_RULES.get(rule_id)
        if rule is None:
            return None
        return getattr(rule, "nb2_ref", None)


__all__ = ["AlertsEngine"]

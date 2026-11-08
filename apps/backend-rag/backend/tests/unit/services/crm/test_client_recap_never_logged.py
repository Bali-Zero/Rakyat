"""The kita AI recap never writes client data to a log record.

Recap sentences and the facts behind them are client data (Builder Contract 4:
logs are outputs), and the exceptions raised around them -- asyncpg, the
providers, the parsers -- echo the offending value in their text, attributes
and chained cause. A log line may say which client, which scope and which
exception TYPE or validator reason code, never any part of the text or value.

Each test pushes one sentinel through a different door and fails if it reaches
any captured record (message, args, extra fields, exception, traceback). Each
also asserts the module did log, so an empty capture cannot pass for a clean one.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.crm import client_recap as r
from backend.services.crm import client_recap_text as rt

SENTINEL = "QQSENTINEL7731"
ROUTER = "backend.app.routers.crm_enhanced"
FACTS = {"passport_expiry_date": "2029-05-05", "family_members_count": 0}


def _exception_text(exc: BaseException, seen: set[int]) -> str:
    if id(exc) in seen:
        return ""
    seen.add(id(exc))
    parts = [repr(exc), repr(exc.args), repr(vars(exc))]
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None:
            parts.append(_exception_text(linked, seen))
    return " ".join(parts)


def _leaks(caplog: pytest.LogCaptureFixture) -> list[str]:
    leaked = []
    for record in caplog.records:
        parts = [record.getMessage(), *(repr(v) for v in vars(record).values())]
        args = record.args if isinstance(record.args, tuple) else (record.args or {},)
        seen: set[int] = set()
        for arg in args:
            if isinstance(arg, BaseException):
                parts.append(_exception_text(arg, seen))
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(_exception_text(record.exc_info[1], seen))
            parts.append(logging.Formatter().formatException(record.exc_info))
        if SENTINEL in " ".join(parts):
            leaked.append(f"{record.name}:{record.lineno} {record.levelname}")
    return leaked


def _logged_from(caplog: pytest.LogCaptureFixture, module: str) -> int:
    return sum(1 for record in caplog.records if record.name == module)


def _failure() -> RuntimeError:
    try:
        raise ValueError(f"cause {SENTINEL}")
    except ValueError as cause:
        failure = RuntimeError(f"value {SENTINEL}")
        failure.__cause__ = cause
    return failure


class _Pool:
    def __init__(self):
        self.conn = MagicMock()
        self.conn.fetchrow = AsyncMock(return_value=None)
        self.conn.fetchval = AsyncMock(return_value=None)
        self.conn.execute = AsyncMock()
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *a):
                return False

        self.acquire = lambda: _Ctx()


class _Provider:
    name = "fake"
    model = "m1"

    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error

    async def generate(self, prompt):
        if self.error is not None:
            raise self.error
        return self.text


async def _drain():
    from backend.services.common import background

    while background._inflight:
        await asyncio.gather(*list(background._inflight))


@pytest.mark.asyncio
async def test_rewrite_exception_logs_type_only(caplog):
    pool = _Pool()
    pool.conn.execute = AsyncMock(side_effect=_failure())
    prov = _Provider(text="The passport expires on 5 May 2029. Nothing else is recorded.")
    with caplog.at_level(logging.DEBUG):
        await r.get_recap(pool, 9101, "immigration", "", FACTS, provider=prov)
        await _drain()
    assert _logged_from(caplog, r.__name__) >= 1
    assert "RuntimeError" in " ".join(rec.getMessage() for rec in caplog.records)
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_rejected_rewrite_logs_reason_codes_never_the_text(caplog):
    pool = _Pool()
    prov = _Provider(text=f"The passport expires on 6 June 2031. {SENTINEL} Renew it soon.")
    with caplog.at_level(logging.DEBUG):
        await r.get_recap(pool, 9102, "immigration", "", FACTS, provider=prov)
        await _drain()
    assert _logged_from(caplog, r.__name__) >= 1
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_lkpm_lookup_failure_logs_type_only(caplog):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"company_type": "PT"})

    async def fetch(sql, *args):
        if "lkpm_reports" in sql:
            raise _failure()
        return []

    conn.fetch = fetch
    with caplog.at_level(logging.DEBUG):
        out = await r.fetch_company_inputs(conn, 55)
    assert out["lkpm_rows"] is None
    assert _logged_from(caplog, r.__name__) == 1
    assert "RuntimeError" in caplog.text
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_visa_catalogue_failure_logs_type_only(caplog):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=False)

    async def fetch(sql, *args):
        if "visa_types" in sql:
            raise _failure()
        return []

    conn.fetch = fetch
    with caplog.at_level(logging.DEBUG):
        out = await r.fetch_immigration_inputs(conn, 9105)
    assert out["catalogue"] is None
    assert _logged_from(caplog, r.__name__) == 1
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_kbli_catalogue_failure_logs_the_type_never_the_code_or_the_server_text(caplog):
    import asyncpg

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=asyncpg.UndefinedTableError(f"relation {SENTINEL}"))
    with caplog.at_level(logging.DEBUG):
        verdicts = await r._kbli_verdicts(conn, {"kbli_code": "62010"})
    assert verdicts == {"62010": "UNKNOWN"}
    assert "UndefinedTableError" in caplog.text
    assert "62010" not in caplog.text
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_endpoint_facts_failure_logs_type_only(caplog):
    from backend.app.routers import crm_enhanced as ce

    pool = _Pool()
    user = {"id": "u", "role": "admin", "email": "a@example.com"}
    with (
        patch.object(ce, "verify_client_access", AsyncMock()),
        patch.object(r, "RECAP_ENABLED", True),
        patch.object(r, "fetch_immigration_inputs", AsyncMock(side_effect=_failure())),
        caplog.at_level(logging.DEBUG),
    ):
        out = await ce.get_client_recap(
            9104, scope="immigration", company_id=None, pool=pool, current_user=user
        )
    assert out["text"] is None
    assert _logged_from(caplog, ROUTER) == 1
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_gemini_provider_failure_logs_type_only(caplog):
    client = MagicMock()
    client.is_available = True
    client.generate_content = AsyncMock(side_effect=_failure())
    with (
        patch("backend.llm.genai_client.GENAI_AVAILABLE", True),
        patch("backend.llm.genai_client.get_genai_client", return_value=client),
        caplog.at_level(logging.DEBUG),
    ):
        text = await rt.GeminiProvider().generate("prompt")
    assert text is None
    assert _logged_from(caplog, rt.__name__) == 1
    assert _leaks(caplog) == []

"""clients.gender has ONE encoding ("M" / "F") on every writer (completes #183).

Synthetic values only. Each cure has a test that fails when the cure is removed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import backend.app.routers.crm_clients_documents as ccd
from backend.app.routers.crm_clients import ClientCreate, ClientUpdate
from backend.services.crm.cache_query import CRMQueryOptimizer
from backend.services.crm.client_core import ClientValidator
from backend.services.crm.gender import normalize_gender, normalize_gender_lenient


class _Ctx:
    def __init__(self, conn: AsyncMock) -> None:
        self.conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self.conn

    async def __aexit__(self, *a: Any) -> None:
        return None


def _pool() -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Ctx(conn))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    return pool, conn


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("M", "M"),
        ("f", "F"),
        ("Male", "M"),
        ("FEMALE", "F"),
        (" male ", "M"),
        ("", None),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize_accepts_and_maps(raw: Any, want: str | None) -> None:
    assert normalize_gender(raw) == want


@pytest.mark.parametrize("raw", ["x", "O", "other", "unknown", "male female", "m" * 40, 5])
def test_normalize_rejects_anything_else(raw: Any) -> None:
    with pytest.raises(ValueError):
        normalize_gender(raw)
    assert normalize_gender_lenient(raw) is None


@pytest.mark.parametrize("model", [ClientCreate, ClientUpdate])
def test_request_models_reject_a_free_text_gender(model: Any) -> None:
    kwargs = {"full_name": "Test Client"} if model is ClientCreate else {}
    assert model(**kwargs, gender="female").gender == "F"
    with pytest.raises(ValidationError):
        model(**kwargs, gender="a-very-long-free-text-value-over-20")


def test_service_validator_normalizes_and_rejects() -> None:
    assert ClientValidator(full_name="Test Client", gender="male").gender == "M"
    with pytest.raises(ValidationError):
        ClientValidator(full_name="Test Client", gender="zz")


@pytest.mark.asyncio
async def test_batch_insert_writes_gender_per_row() -> None:
    pool, conn = _pool()
    conn.fetch = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    rows = [{"full_name": "A Client", "gender": "female"}, {"full_name": "B Client"}]
    with patch("backend.services.crm.cache_query.lock_phone_cores", new=AsyncMock()):
        await CRMQueryOptimizer(pool).batch_insert_clients(rows)
    query, *params = conn.fetch.await_args.args
    assert "gender" in query and "$22" in query, "11 placeholders per row expected"
    assert len(params) == 22
    assert params[10] == "F" and params[21] is None


@pytest.mark.parametrize(("read", "stored"), [("M", "M"), ("female", "F"), ("X", None)])
@pytest.mark.asyncio
async def test_ocr_persist_stores_M_or_F_and_skips_a_misread(read: str, stored: str | None) -> None:
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"full_name": "Test Client"})
    request = ccd.PassportPreviewRequest(image_base64="aGk=", mime_type="image/jpeg", client_id=7)
    reply = MagicMock(status_code=200)
    reply.json.return_value = {"response": "{}"}
    extracted = {
        "passport_number": "X0000000",
        "gender": read,
        "full_name": "Test Client",
        "confidence": 0.9,
    }
    http = AsyncMock()
    http.post = AsyncMock(return_value=reply)
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("httpx.AsyncClient", return_value=http),
        patch.object(ccd, "extract_json_from_llm_response", return_value=extracted),
        patch.object(ccd, "verify_client_access", new=AsyncMock()),
        patch.object(ccd, "invalidate_cache", new=AsyncMock()),
    ):
        out = await ccd.extract_passport_enhanced(
            request=request, current_user={"id": "u", "role": "admin", "email": "t@x"}, db_pool=pool
        )
    assert out.success, out.message
    sql, *params = conn.execute.await_args.args
    assert ("gender = $" in sql) == (stored is not None)
    if stored:
        assert stored in params


@pytest.mark.asyncio
async def test_core_create_client_writes_gender() -> None:
    from backend.services.crm.client_core import EnhancedCRMService

    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"id": 3, "full_name": "Test Client"})
    with (
        patch("backend.services.crm.client_core.CRMQueryOptimizer"),
        patch("backend.services.crm.client_core.lock_phone_cores", new=AsyncMock()),
    ):
        svc = EnhancedCRMService(pool)
        svc.auditor = MagicMock(log_client_created=AsyncMock())
        svc._find_duplicate_client = AsyncMock(return_value=None)
        await svc.create_client({"full_name": "Test Client", "gender": "female"})
    query, *params = conn.fetchrow.await_args.args
    assert "gender" in query and "$11" in query
    assert params[-1] == "F"


@pytest.mark.parametrize(("read", "stored"), [("MALE", "M"), ("F", "F"), ("L", None)])
@pytest.mark.asyncio
async def test_auto_ocr_passport_stores_M_or_F_and_skips_a_misread(
    read: str, stored: str | None
) -> None:
    import backend.app.routers.crm_enhanced as enhanced

    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"full_name": "Test Client"})
    with (
        patch.object(
            enhanced, "_download_drive_file", new=AsyncMock(return_value=(b"x", "image/jpeg"))
        ),
        patch.object(enhanced, "_gemini_ocr", new=AsyncMock(return_value="{}")),
        patch.object(
            enhanced,
            "extract_json_from_llm_response",
            return_value={
                "passport_number": "X0000000",
                "gender": read,
                "full_name": "Test Client",
            },
        ),
    ):
        out = await enhanced._auto_ocr_passport(pool, 7, "file-1")
    assert out["success"], out
    sql, *params = conn.execute.await_args.args
    assert ("gender = $" in sql) == (stored is not None)
    if stored:
        assert stored in params


async def _update(updates: dict[str, Any], stored_gender: str | None) -> AsyncMock:
    from backend.services.crm.client_core import EnhancedCRMService

    pool, conn = _pool()
    old = {"id": 1, "full_name": "Old Name", "gender": stored_gender, "status": "active"}
    conn.fetchrow = AsyncMock(side_effect=[old, {**old, **updates}])
    with (
        patch("backend.services.crm.client_core.CRMQueryOptimizer"),
        patch("backend.services.crm.client_core.invalidate_client_cache"),
    ):
        svc = EnhancedCRMService(pool)
        svc.auditor = MagicMock(log_client_updated=AsyncMock())
        await svc.update_client(1, updates)
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["L", "male", "x" * 19, None, "M"])
async def test_editing_a_name_never_fails_on_the_stored_gender(stored: str | None) -> None:
    conn = await _update({"full_name": "New Name"}, stored_gender=stored)
    query, *params = conn.fetchrow.await_args.args
    assert "full_name = $1" in query and "gender" not in query
    assert params == ["New Name", 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sex", "stored"),
    [("M", "M"), (" Female ", "F"), ("X", None), ("male female", None), ("", None)],
)
async def test_batch_passport_script_writes_only_m_or_f(sex: str, stored: str | None) -> None:
    """The script needs OAuth env at import, so gender_letter and update_client are loaded alone."""
    import ast
    import logging
    from pathlib import Path

    path = Path(__file__).resolve().parents[5] / "scripts" / "batch_passport_ocr.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"gender_letter", "update_client"}
    fns = [
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name in wanted
    ]
    ns: dict[str, Any] = {
        "asyncpg": MagicMock(),
        "normalize_date": lambda value: None,
        "logger": logging.getLogger("t"),
    }
    exec(compile(ast.Module(fns, []), str(path), "exec"), ns)  # noqa: S102
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    await ns["update_client"](conn, 5, {"sex": sex}, dry_run=False)
    writes = [c.args for c in conn.execute.await_args_list if "SET gender" in c.args[0]]
    assert [w[1] for w in writes] == ([stored] if stored else [])


@pytest.mark.asyncio
async def test_passport_preview_returns_canonical_or_nothing() -> None:
    pool, _ = _pool()
    request = ccd.PassportPreviewRequest(image_base64="aGk=", mime_type="image/jpeg")
    reply = MagicMock(status_code=200)
    reply.json.return_value = {"response": "{}"}
    http = AsyncMock()
    http.post = AsyncMock(return_value=reply)
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    seen = []
    for read in ("Female", "X", "male female"):
        extracted = {"passport_number": "X0000000", "gender": read, "full_name": "Test Client"}
        with (
            patch("httpx.AsyncClient", return_value=http),
            patch.object(ccd, "extract_json_from_llm_response", return_value=extracted),
        ):
            out = await ccd.extract_passport_enhanced(
                request=request,
                current_user={"id": "u", "role": "admin", "email": "t@x"},
                db_pool=pool,
            )
        seen.append(out.gender)
    assert seen == ["F", None, None]


@pytest.mark.asyncio
async def test_auto_ocr_passport_returns_canonical_or_nothing() -> None:
    import backend.app.routers.crm_enhanced as enhanced

    results = []
    for read in ("MALE", "L"):
        pool, conn = _pool()
        conn.fetchrow = AsyncMock(return_value={"full_name": "Test Client"})
        with (
            patch.object(
                enhanced, "_download_drive_file", new=AsyncMock(return_value=(b"x", "image/jpeg"))
            ),
            patch.object(enhanced, "_gemini_ocr", new=AsyncMock(return_value="{}")),
            patch.object(
                enhanced,
                "extract_json_from_llm_response",
                return_value={
                    "passport_number": "X0000000",
                    "gender": read,
                    "full_name": "Test Client",
                },
            ),
        ):
            out = await enhanced._auto_ocr_passport(pool, 7, "file-1")
        results.append(out["extracted"]["gender"])
    assert results == ["M", None]

"""DAI-156: the tax team may name the invoice company on a NEW process.

``POST /api/crm/practices`` with ``invoice_company_id``/``invoice_company_name``
resolved the company through ``_resolve_invoice_company_http``, which demanded
write access to the client. A tax lead creating a process for a client they do
not own got 403 and the whole process was rolled back.

The fix is an explicit opt-in (``allow_tax_create``) that only ``create_practice``
passes: a ``can_create_company`` role skips the client-ownership write check on
create, but the client must still exist (404). PATCH keeps the owner/admin rule.

These tests drive the REAL ``verify_client_access`` / ``can_write_client`` with a
mocked asyncpg connection; only the company resolver itself is stubbed. The
end-to-end "practice + company + link saved" proof against PostgreSQL lives in
``tests/db/test_invoice_company_production_shape_pg.py``.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.app.routers import crm_practices
from backend.app.utils import crm_utils

OWNER = "synthetic.owner@example.invalid"
TAX_LEAD = {"email": "synthetic.taxlead@example.invalid", "role": "Tax Lead"}
CONSULTANT = {"email": "synthetic.consultant@example.invalid", "role": "consultant"}
OWNER_USER = {"email": OWNER, "role": "consultant"}
ADMIN = {"email": "synthetic.admin@example.invalid", "role": "admin"}
RESOLVED_COMPANY_ID = 4242


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_a: object) -> None:
        return None


def _conn(client_row: dict | None) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=client_row)
    conn.transaction = MagicMock(return_value=_Tx())
    return conn


def _client_row() -> dict:
    return {"id": 12, "assigned_to": OWNER, "created_by": OWNER}


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def _validated(_conn: object) -> None:
        return None

    monkeypatch.setattr(crm_utils, "ensure_client_pools_validated", _validated)
    stub = AsyncMock(return_value=RESOLVED_COMPANY_ID)
    monkeypatch.setattr(crm_practices.invoice_company_service, "resolve_invoice_company", stub)
    return stub


async def _resolve(conn: MagicMock, user: dict, **kw: object) -> int:
    # Before DAI-156 the helper had no ``allow_tax_create`` and every caller got
    # the owner/admin rule; dropping the kwarg there reproduces that behaviour so
    # the same tests run (red/green) against the old code.
    params = inspect.signature(crm_practices._resolve_invoice_company_http).parameters
    kw = {k: v for k, v in kw.items() if k in params}
    return await crm_practices._resolve_invoice_company_http(
        conn,
        12,
        company_id=None,
        name="PT Synthetic Pajak",
        npwp=None,
        current_user=user,
        **kw,
    )


# --- guard: red on main, green with the fix --------------------------------


@pytest.mark.asyncio
async def test_tax_lead_names_invoice_company_on_new_process_for_unowned_client(
    resolver: AsyncMock,
) -> None:
    out = await _resolve(_conn(_client_row()), TAX_LEAD, allow_tax_create=True)
    assert out == RESOLVED_COMPANY_ID
    resolver.assert_awaited_once()
    assert resolver.await_args.kwargs["actor"] == TAX_LEAD["email"]


def _resolve_calls_in(func_name: str) -> list[ast.Call]:
    source = textwrap.dedent(inspect.getsource(getattr(crm_practices, func_name)))
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_resolve_invoice_company_http"
    ]


def _passes_tax_create(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "allow_tax_create":
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def test_create_practice_opts_into_tax_create() -> None:
    calls = _resolve_calls_in("create_practice")
    assert len(calls) == 1
    assert _passes_tax_create(calls[0])


# --- innocence: green on main and with the fix ------------------------------


def test_update_practice_does_not_opt_into_tax_create() -> None:
    calls = _resolve_calls_in("update_practice")
    assert len(calls) == 1
    assert not _passes_tax_create(calls[0])


@pytest.mark.asyncio
async def test_tax_lead_on_patch_path_still_needs_write_access(resolver: AsyncMock) -> None:
    # The PATCH caller does not pass allow_tax_create → default False.
    with pytest.raises(HTTPException) as caught:
        await _resolve(_conn(_client_row()), TAX_LEAD)
    assert caught.value.status_code == 403
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_tax_role_on_new_process_still_needs_write_access(resolver: AsyncMock) -> None:
    with pytest.raises(HTTPException) as caught:
        await _resolve(_conn(_client_row()), CONSULTANT, allow_tax_create=True)
    assert caught.value.status_code == 403
    resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [OWNER_USER, ADMIN], ids=["owner", "admin"])
@pytest.mark.parametrize("allow_tax_create", [False, True], ids=["patch", "create"])
async def test_owner_and_admin_still_resolve(
    resolver: AsyncMock, user: dict, allow_tax_create: bool
) -> None:
    out = await _resolve(_conn(_client_row()), user, allow_tax_create=allow_tax_create)
    assert out == RESOLVED_COMPANY_ID


@pytest.mark.asyncio
async def test_tax_lead_on_missing_client_is_404(resolver: AsyncMock) -> None:
    with pytest.raises(HTTPException) as caught:
        await _resolve(_conn(None), TAX_LEAD, allow_tax_create=True)
    assert caught.value.status_code == 404
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_member_without_tax_role_on_new_process_is_still_403(
    resolver: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A client-pool member (peer set > 1) who is not the client's owner gets no
    # tax exception: DAI-156 is for tax roles only, whatever pool membership
    # ``can_create_company`` may later accept.
    from backend.app.utils import client_pools

    reception = {"email": "synthetic.reception@example.invalid", "role": "reception"}
    peers = frozenset({reception["email"], "synthetic.poolpeer@example.invalid"})
    monkeypatch.setattr(client_pools, "caller_pool_peers", lambda _user: peers)

    with pytest.raises(HTTPException) as caught:
        await _resolve(_conn(_client_row()), reception, allow_tax_create=True)
    assert caught.value.status_code == 403
    resolver.assert_not_awaited()

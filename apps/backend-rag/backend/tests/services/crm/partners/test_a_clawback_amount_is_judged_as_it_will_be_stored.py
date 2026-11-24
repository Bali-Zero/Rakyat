"""A clawback amount is judged as the ledger will store it, whatever the caller's decimal context.

The ledger column is NUMERIC(16,2), rounded half away from zero. Real test database (the
partners fixtures); invented rows only.
"""

from __future__ import annotations

import sys
import uuid
from decimal import (
    ROUND_HALF_EVEN,
    Decimal,
    FloatOperation,
    Inexact,
    Rounded,
    localcontext,
)

import pytest

from backend.services.crm.partners.commission_engine import CommissionEngine

_NET = Decimal("980000.00")
_REFUSAL = (
    "clawback amount must be greater than 0 and at most the commission net amount (980000.00)"
)


@pytest.fixture
async def engine(db_conn):
    return CommissionEngine(db_conn)


async def _paid_commission(engine, partner_factory, *, net: Decimal = _NET) -> uuid.UUID:
    partner = await partner_factory()
    return await engine.repo.insert_commission(
        partner_id=partner.id,
        entry_type="accrual",
        base_amount_idr=Decimal("10000000"),
        commission_type_snapshot="percentage",
        commission_value_snapshot=Decimal("10.0"),
        gross_amount_idr=Decimal("1000000"),
        net_amount_idr=net,
        status="paid",
        idempotency_key=f"clawback-amount-{uuid.uuid4().hex}",
    )


async def _clawbacks(db_conn, original: uuid.UUID) -> int:
    return await db_conn.fetchval(
        "SELECT count(*) FROM partner_commissions WHERE related_commission_id = $1", original
    )


_REFUSED = [
    Decimal("-500000"),
    Decimal("0"),
    Decimal("-0"),
    Decimal("0.001"),
    Decimal("0.0049"),
    Decimal("980000.01"),
    Decimal("980000.005"),
    Decimal("1E+30"),
    Decimal("NaN"),
    Decimal("Infinity"),
]

_ACCEPTED = [
    (Decimal("500000"), Decimal("-500000.00")),
    (Decimal("980000"), Decimal("-980000.00")),
    (Decimal("0.005"), Decimal("-0.01")),
    (Decimal("979999.995"), Decimal("-980000.00")),
    (None, Decimal("-980000.00")),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", _REFUSED, ids=str)
async def test_an_amount_outside_the_commission_is_refused_and_nothing_is_written(
    engine, partner_factory, admin, db_conn, amount
):
    original = await _paid_commission(engine, partner_factory)
    with pytest.raises(ValueError) as exc:
        await engine.clawback(original, actor=admin, reason="refund", amount_idr=amount)
    # The message names the rule and the stored net, never the amount asked for.
    assert str(exc.value) == _REFUSAL
    assert await _clawbacks(db_conn, original) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("amount, stored", _ACCEPTED, ids=str)
async def test_an_amount_inside_the_commission_is_stored_as_the_ledger_rounds_it(
    engine, partner_factory, admin, amount, stored
):
    original = await _paid_commission(engine, partner_factory)
    clawback = await engine.clawback(original, actor=admin, reason="refund", amount_idr=amount)
    row = await engine.repo.get_commission(clawback)
    assert row.gross_amount_idr == stored
    assert row.net_amount_idr == stored


@pytest.mark.asyncio
async def test_an_integer_too_long_to_print_is_refused_like_any_other_amount(
    engine, partner_factory, admin, db_conn
):
    original = await _paid_commission(engine, partner_factory)
    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with pytest.raises(ValueError) as exc:
            await engine.clawback(original, actor=admin, reason="refund", amount_idr=10**5000)
    finally:
        sys.set_int_max_str_digits(old)
    assert str(exc.value) == _REFUSAL
    assert await _clawbacks(db_conn, original) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("net", [Decimal("NaN"), Decimal("0")], ids=str)
@pytest.mark.parametrize("amount", [Decimal("0.01"), None], ids=str)
async def test_a_commission_whose_net_is_not_a_positive_amount_refuses_every_clawback(
    engine, partner_factory, admin, db_conn, net, amount
):
    original = await _paid_commission(engine, partner_factory, net=net)
    with pytest.raises(ValueError):
        await engine.clawback(original, actor=admin, reason="refund", amount_idr=amount)
    assert await _clawbacks(db_conn, original) == 0


_HOSTILE = [
    (Decimal("-500000"), None),
    (Decimal("0.001"), None),
    (Decimal("0.0049"), None),
    (Decimal("0.005"), Decimal("-0.01")),
    (Decimal("979999.995"), Decimal("-980000.00")),
    (Decimal("980000.005"), None),
    (Decimal("1E+30"), None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("amount, stored", _HOSTILE, ids=str)
async def test_the_callers_decimal_context_changes_nothing(
    engine, partner_factory, admin, db_conn, amount, stored
):
    original = await _paid_commission(engine, partner_factory)
    refused = False
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = ROUND_HALF_EVEN
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        ctx.traps[FloatOperation] = True
        try:
            clawback = await engine.clawback(
                original, actor=admin, reason="refund", amount_idr=amount
            )
        except ValueError:
            refused = True
    if stored is None:
        assert refused
        assert await _clawbacks(db_conn, original) == 0
    else:
        assert not refused
        row = await engine.repo.get_commission(clawback)
        assert row.gross_amount_idr == stored
        assert row.net_amount_idr == stored

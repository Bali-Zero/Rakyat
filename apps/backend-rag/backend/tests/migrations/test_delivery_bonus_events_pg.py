"""Exercise the real issuance trigger and payroll writer on an isolated PostgreSQL schema."""

from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers.hr import delivery_bonus_reviews
from backend.app.services.hr import delivery_bonus as bonus
from backend.app.services.hr.delivery_bonus import (
    delivery_desk,
    record_delivery_bonus,
    resolve_delivery_bonus,
)
from backend.tests.migrations.hr_bonus_fixture import install_delivery_payroll_migrations
from backend.tests.migrations.test_migration_357_hr_bonus_manual import _DDL, _dsn, _parts


@pytest_asyncio.fixture
async def db():
    conn = await asyncpg.connect(_dsn())
    transaction = conn.transaction()
    await transaction.start()
    try:
        schema = "delivery_" + uuid4().hex
        await conn.execute(f"CREATE SCHEMA {schema}; SET LOCAL search_path TO {schema}")
        await conn.execute(_DDL)
        await conn.execute(_parts()[0])
        await conn.execute("""
            ALTER TABLE team_members ADD COLUMN email text, ADD COLUMN full_name text, ADD COLUMN role text, ADD COLUMN active boolean DEFAULT true;
            ALTER TABLE hr_employees ADD COLUMN is_active boolean DEFAULT true, ADD COLUMN profile jsonb;
            CREATE TABLE hr_bonus_rates(id serial PRIMARY KEY, practice_type_code text UNIQUE,
                is_active boolean, effective_from date, effective_to date, amount_idr bigint);
            CREATE TABLE hr_payroll_periods(id serial PRIMARY KEY, period_start date, payroll_month int, payroll_year int, status text);
            CREATE TABLE practice_types(id serial PRIMARY KEY, code text, name text);
            ALTER TABLE practices ADD COLUMN assigned_to text, ADD COLUMN practice_type_id int,
                ADD COLUMN start_date timestamptz, ADD COLUMN created_at timestamptz DEFAULT now(), ADD COLUMN payment_status text;
            ALTER TABLE hr_bonus_ledger ADD COLUMN bonus_rate_id int REFERENCES hr_bonus_rates(id), ADD COLUMN payroll_period_id integer;
            UPDATE team_members SET email='consultant@example.invalid',role='consultant';
            INSERT INTO practice_types(code,name) VALUES ('visa_demo','Example new visa');
            INSERT INTO hr_bonus_rates(practice_type_code,is_active,effective_from,amount_idr)
                VALUES ('visa_demo',true,'2020-01-01',125000);
            UPDATE practices SET assigned_to='consultant@example.invalid', practice_type_id=1, payment_status='paid';
            UPDATE practices SET status='on_process' WHERE id=2;
        """)
        await install_delivery_payroll_migrations(conn)
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_issuance_pays_once_before_completion_reassignment_cannot_duplicate_it(db):
    issued = await record_delivery_bonus(
        db,
        2,
        "visa_demo",
        "consultant@example.invalid",
        "electronic_visa_issued",
        issued_on=date(2020, 12, 29),
    )
    assert issued["amount_idr"] == 125000 and issued["status"] == "pending"
    assert issued["earned_at"] == issued["recorded_at"]
    assert issued["evidence_issued_on"] == date(2020, 12, 29)
    assert issued["awarded_at"] == bonus.payroll_award_date(issued["recorded_at"])
    assert await db.fetchval("SELECT status FROM practices WHERE id=2") == "on_process"
    assert (await delivery_desk(db, "tm-1"))["next_case"]["already_earned"]
    await db.execute("UPDATE practices SET status='completed' WHERE id=2")
    replay = await record_delivery_bonus(
        db, 2, "visa_demo", "replacement@example.invalid", "service_delivered"
    )
    assert replay == issued
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=2") == 1


@pytest.mark.asyncio
async def test_missing_rate_is_retained_and_explicitly_resolved_once(db):
    await db.execute("UPDATE practices SET status='completed' WHERE id=2")
    result = await record_delivery_bonus(
        db, 2, "tax_demo", "consultant@example.invalid", "service_delivered"
    )
    assert result["status"] == "awaiting_rate" and result["amount_idr"] is None
    assert (await delivery_desk(db, "tm-1"))["recent"][0]["practice_id"] == 2
    assert (await resolve_delivery_bonus(db, 2, 250000, "hr-reviewer"))["amount_idr"] == 250000
    await resolve_delivery_bonus(db, 2, 250000, "hr-reviewer")
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=2") == 1


@pytest.mark.asyncio
async def test_legacy_award_survives_upgrade_and_unqualified_insert_is_rejected(db):
    result = await record_delivery_bonus(
        db, 1, "visa_fake", "replacement@example.invalid", "service_delivered"
    )
    assert result["amount_idr"] == 100
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=1") == 1
    with pytest.raises(asyncpg.RaiseError):
        async with db.transaction():
            await db.execute("""INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr)
                            VALUES(2,1,'visa_demo',100)""")


@pytest.mark.asyncio
async def test_unattributed_issuance_requires_hr_selection(db):
    result = await record_delivery_bonus(
        db, 2, "visa_demo", None, "electronic_visa_issued", issued_on=date(2020, 12, 29)
    )
    assert result["status"] == "awaiting_employee"
    with pytest.raises(ValueError, match="Select the employee"):
        await resolve_delivery_bonus(db, 2, 125000, "hr-reviewer")
    resolved = await resolve_delivery_bonus(db, 2, 125000, "hr-reviewer", employee_id=1)
    assert resolved["status"] == "pending"
    assert (
        await db.fetchval(
            "SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=2 AND employee_id=1"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_renewed_imk_upload_earns_before_completion_and_freezes_its_original_evidence(db):
    first = await record_delivery_bonus(
        db,
        2,
        "visa_demo",
        "consultant@example.invalid",
        "kitas_imk_uploaded",
        issued_on=date(2020, 12, 29),
    )
    assert first["status"] == "pending" and first["event_kind"] == "kitas_imk_uploaded"
    assert first["earned_at"] == first["recorded_at"]
    assert first["evidence_issued_on"] == date(2020, 12, 29)
    assert await db.fetchval("SELECT status FROM practices WHERE id=2") == "on_process"
    later = await record_delivery_bonus(
        db, 2, "visa_demo", "replacement@example.invalid", "service_delivered"
    )
    assert later == first
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=2") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exclusion", ["card_flag", "inactive_login", "client_role"])
async def test_known_ineligible_assignee_cannot_earn_after_reassignment_or_later_eligibility(
    db, exclusion
):
    await db.execute("UPDATE practices SET status='completed' WHERE id=2")
    await db.execute("""INSERT INTO team_members(id,email,role,active)
                        VALUES('excluded','excluded@example.invalid','consultant',TRUE)""")
    excluded = await db.fetchval(
        "INSERT INTO hr_employees(team_member_id) VALUES('excluded') RETURNING id"
    )
    if exclusion == "card_flag":
        await db.execute(
            """UPDATE hr_employees SET profile='{"on_payroll":false}' WHERE id=$1""", excluded
        )
    elif exclusion == "inactive_login":
        await db.execute("UPDATE team_members SET active=FALSE WHERE id='excluded'")
    else:
        await db.execute("UPDATE team_members SET role='client' WHERE id='excluded'")

    first = await record_delivery_bonus(
        db, 2, "visa_demo", " EXCLUDED@example.invalid ", "service_delivered"
    )
    assert first["status"] == "ineligible" and first["amount_idr"] is None
    claim = await db.fetchrow("SELECT * FROM hr_delivery_bonus_events WHERE practice_id=2")
    assert claim["payroll_ineligible"] and claim["original_team_member_id"] == "excluded"
    assert claim["employee_id"] is None and claim["ledger_id"] is None

    await db.execute("UPDATE hr_employees SET profile='{}' WHERE id=$1", excluded)
    await db.execute("UPDATE team_members SET active=TRUE,role='consultant' WHERE id='excluded'")
    replay = await record_delivery_bonus(
        db, 2, "visa_demo", "consultant@example.invalid", "service_delivered"
    )
    assert replay == first
    for candidate in (excluded, 1):
        with pytest.raises(ValueError, match="outside payroll at delivery"):
            await resolve_delivery_bonus(db, 2, 125000, "hr-reviewer", employee_id=candidate)
    with pytest.raises(asyncpg.RaiseError, match="outside payroll at delivery"):
        async with db.transaction():
            await db.execute("""INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr)
                                VALUES (2,1,'visa_demo',125000)""")
    assert not await db.fetchval("SELECT EXISTS(SELECT 1 FROM hr_bonus_ledger WHERE practice_id=2)")
    manual_id = await db.fetchval(
        """INSERT INTO hr_bonus_ledger(employee_id,practice_type_code,amount_idr,source,reason)
           VALUES (1,'manual',50,'manual','Unrelated staff award') RETURNING id"""
    )
    assert manual_id is not None


@pytest.mark.asyncio
async def test_identified_member_missing_card_can_only_be_resolved_to_their_own_new_hr_card(db):
    # The People predicate recognises a named staff role with a company mailbox before a card exists.
    email = "fixture-cardless@balizero.com"
    await db.execute(
        "INSERT INTO team_members(id,email,role,active) VALUES('cardless',$1,'consultant',TRUE)",
        email,
    )
    await db.execute("UPDATE practices SET status='completed' WHERE id=2")
    receipt = await record_delivery_bonus(db, 2, "visa_demo", email, "service_delivered")
    assert receipt["status"] == "awaiting_employee"
    with pytest.raises(ValueError, match="original beneficiary"):
        await resolve_delivery_bonus(db, 2, 125000, "hr-reviewer", employee_id=1)
    matching = await db.fetchval(
        "INSERT INTO hr_employees(team_member_id) VALUES('cardless') RETURNING id"
    )
    assert (await resolve_delivery_bonus(db, 2, 125000, "hr-reviewer", employee_id=matching))[
        "status"
    ] == "pending"


@pytest.mark.asyncio
async def test_excluded_claims_do_not_displace_unattributed_claims_from_hr_review_queue(db):
    await db.execute("""UPDATE hr_employees SET profile='{"on_payroll":false}' WHERE id=1""")
    await db.execute(
        "INSERT INTO practices(id,status) SELECT n,'completed' FROM generate_series(3,104) n"
    )
    for practice_id in range(3, 104):
        await record_delivery_bonus(
            db, practice_id, "visa_demo", "consultant@example.invalid", "service_delivered"
        )
    review = await record_delivery_bonus(
        db, 104, "visa_demo", "unknown@example.invalid", "service_delivered"
    )
    assert review["status"] == "awaiting_employee"
    # Existing paid/approved/reversed history takes priority even when the original assignee is now excluded.
    legacy = await record_delivery_bonus(
        db, 1, "visa_demo", "consultant@example.invalid", "service_delivered"
    )
    assert legacy["amount_idr"] == 100 and legacy["status"] != "ineligible"
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = db
    response = await delivery_bonus_reviews({"role": "admin", "user_id": "reviewer"}, pool)
    assert [row["practice_id"] for row in response["items"]] == [104]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "slot", "label"),
    [
        ("visa_c1_tourism", "document_2", "ITK — Visit Stay Permit · after confirmed entry"),
        (
            "kitas_dependent_1yr_offshore",
            "document_3",
            "IMK — Re-entry Permit · after confirmed entry",
        ),
        ("legacy_unknown", "document_2", None),
        ("visa_c1_tourism", "retired_slot", None),
    ],
)
async def test_employee_hold_uses_service_catalog_document(db, service, slot, label):
    await record_delivery_bonus(
        db,
        2,
        service,
        "consultant@example.invalid",
        "electronic_visa_issued",
        issued_on=date.today(),
    )
    await db.execute(
        """INSERT INTO practice_visa_followups(practice_id, service_code, slot_key, started_on, due_on, allow_postpone)
           VALUES (2, $1, $2, CURRENT_DATE - 35, CURRENT_DATE - 5, true)""",
        service,
        slot,
    )
    (held,) = (await delivery_desk(db, "tm-1"))["holds"]
    assert held["slot_key"] == slot
    assert held["slot_label"] == label

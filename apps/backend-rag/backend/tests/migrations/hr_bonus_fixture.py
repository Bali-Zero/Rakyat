"""Install the production bonus/permit migrations inside the payroll tests' private schema."""

from pathlib import Path

from backend.db.migration_base import split_migration_sql


async def install_delivery_payroll_migrations(conn, *, forfeit_rule: bool = True) -> None:
    # Match the columns read by the real document and follow-up triggers.
    await conn.execute("""
        ALTER TABLE practices ADD COLUMN IF NOT EXISTS client_id integer;
        ALTER TABLE practices ADD COLUMN IF NOT EXISTS family_member_id integer;
        ALTER TABLE practices ADD COLUMN IF NOT EXISTS practice_type_id integer;
        CREATE TABLE IF NOT EXISTS practice_types(id serial PRIMARY KEY, code text, name text);
        ALTER TABLE practice_types ADD COLUMN IF NOT EXISTS category text;
        CREATE TABLE companies(id integer PRIMARY KEY);
        CREATE TABLE company_documents(id integer PRIMARY KEY);
        CREATE TABLE documents(id serial PRIMARY KEY, practice_id integer, client_id integer,
            family_member_id integer, file_id text, is_archived boolean, deleted_at timestamptz, status text);
    """)
    dir_path = Path(__file__).resolve().parents[2] / "db/migrations_v2"
    for name in (
        "383_practice_delivery.sql",
        "384_delivery_bonus.sql",
        "385_practice_visa_followups.sql",
        "386_visa_bonus_payout_hold.sql",
        "393_bonus_run_month_lock.sql",
    ):
        await conn.execute(split_migration_sql((dir_path / name).read_text())[0])
    # The columns and table read by 388's forfeit rule (production has them already).
    await conn.execute("""
        ALTER TABLE practices ADD COLUMN IF NOT EXISTS quoted_price numeric(12,2);
        ALTER TABLE practices ADD COLUMN IF NOT EXISTS discount_amount numeric(12,2);
        CREATE TABLE IF NOT EXISTS invoices(id serial PRIMARY KEY, practice_id integer, amount_idr numeric);
        ALTER TABLE hr_bonus_rates ADD COLUMN IF NOT EXISTS notes text;
        ALTER TABLE hr_bonus_rates ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();
        CREATE UNIQUE INDEX IF NOT EXISTS hr_bonus_rates_code_key ON hr_bonus_rates(practice_type_code);
    """)
    if forfeit_rule:
        await apply_forfeit_migration(conn)


async def apply_forfeit_migration(conn) -> None:
    path = Path(__file__).resolve().parents[2] / "db/migrations_v2/388_bonus_rates_forfeit.sql"
    await conn.execute(split_migration_sql(path.read_text())[0])

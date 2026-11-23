#!/usr/bin/env python3
"""
One-shot backfill: create team_members records (role='client') for all
existing CRM clients that don't have one yet.

Every profile is written by `PortalProfileService.ensure_portal_profile`, the
same statement and the same guard the live provisioning uses: a login linked to
another client, or registered, is never moved to the client being backfilled.

Usage:
    cd apps/backend-rag
    source .venv/bin/activate
    PYTHONPATH=. python scripts/backfill_portal_profiles.py [--dry-run]
"""

import argparse
import asyncio
import os
import sys

import asyncpg


async def backfill(pool: asyncpg.Pool, *, dry_run: bool = False) -> dict[str, int]:
    """Provision the missing portal profiles; return the counts it printed."""
    from backend.services.portal.portal_profile_service import PortalProfileService

    async with pool.acquire() as conn:
        total_clients = await conn.fetchval(
            "SELECT COUNT(*) FROM clients WHERE deleted_at IS NULL",
        )
        existing_portal = await conn.fetchval(
            "SELECT COUNT(*) FROM team_members WHERE role = 'client' AND linked_client_id IS NOT NULL",
        )

        print(f"Total active clients: {total_clients}")
        print(f"Existing portal profiles: {existing_portal}")
        print(f"Missing: {total_clients - existing_portal}")
        print()

        # Find clients without portal profile
        missing = await conn.fetch(
            """
            SELECT c.id, c.email, c.full_name
            FROM clients c
            WHERE c.deleted_at IS NULL
              AND c.email IS NOT NULL
              AND TRIM(c.email) != ''
              AND NOT EXISTS (
                SELECT 1 FROM team_members tm
                WHERE tm.linked_client_id = c.id AND tm.role = 'client'
              )
            ORDER BY c.id
            """,
        )

    print(f"Clients to backfill (with valid email): {len(missing)}")

    if dry_run:
        print("\n[DRY RUN] Would create portal profiles for:")
        for row in missing[:10]:
            print(f"  - {row['full_name']} ({row['email']}) [id={row['id']}]")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")
        return {"to_backfill": len(missing), "created": 0, "skipped": 0}

    service = PortalProfileService(pool)
    created = 0
    skipped = 0
    for row in missing:
        member_id = await service.ensure_portal_profile(
            client_id=row["id"],
            email=row["email"],
            full_name=row["full_name"] or "Unknown",
        )
        if member_id:
            created += 1
        else:
            skipped += 1

    print("\nResults:")
    print(f"  Created or linked: {created}")
    print(f"  Skipped (address held by another account, or failed; see the log): {skipped}")

    async with pool.acquire() as conn:
        final_count = await conn.fetchval(
            "SELECT COUNT(*) FROM team_members WHERE role = 'client' AND linked_client_id IS NOT NULL",
        )
    print(f"\nFinal portal profiles: {final_count}")
    return {"to_backfill": len(missing), "created": created, "skipped": skipped}


async def main(dry_run: bool = False) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    try:
        await backfill(pool, dry_run=dry_run)
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill portal profiles for existing clients")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without doing it"
    )
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run))

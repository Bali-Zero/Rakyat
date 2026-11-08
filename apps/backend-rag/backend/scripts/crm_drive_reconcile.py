"""Inspect approved CRM roots or file ownership, and reconcile canonical maps."""

import argparse
import asyncio
import json
import logging
import re
import socket
import sys

from backend.services.crm.drive_taxonomy_repair import (
    RepairError,
    inspect_files,
    reconcile,
    valid_ids,
)


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise RepairError("invalid_arguments")


async def run(args):
    if args.file_id:
        if (
            not valid_ids(args.file_id)
            or args.root_folder_id
            or args.entity_kind
            or args.apply
            or args.expected_plan_sha256
        ):
            raise RepairError("invalid_arguments")
    elif (
        not valid_ids(args.root_folder_id)
        or not args.entity_kind
        or args.apply != bool(args.expected_plan_sha256)
    ):
        raise RepairError("invalid_arguments")
    if args.expected_plan_sha256 and not re.fullmatch(r"[a-f0-9]{64}", args.expected_plan_sha256):
        raise RepairError("invalid_arguments")
    from backend.app.core.config import settings
    from backend.app.core.database import DatabasePoolPolicy, create_database_pool
    from backend.services.integrations.service_account_drive_service import (
        ServiceAccountDriveService,
    )

    if not settings.database_url:
        raise RepairError("configuration_missing")
    parent = None
    if not args.file_id:
        parent = getattr(
            settings,
            "gdrive_individuals_folder_id"
            if args.entity_kind == "individual"
            else "gdrive_companies_folder_id",
            None,
        )
        if not parent:
            raise RepairError("configuration_missing")
    pool = await create_database_pool(
        dsn=settings.database_url,
        policy=DatabasePoolPolicy(
            min_size=1, max_size=2, command_timeout=60, max_inactive_connection_lifetime=300
        ),
    )
    try:
        # googleapiclient's httplib2 honors the process default timeout when
        # constructing its transport, including the constructor's validation GETs.
        socket.setdefaulttimeout(10)
        drive = ServiceAccountDriveService().service
        if args.file_id:
            return await inspect_files(pool, drive, args.file_id)
        return await reconcile(
            pool, drive, args.root_folder_id, parent, args.entity_kind, args.expected_plan_sha256
        )
    finally:
        await pool.close()


def main():
    previous_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--entity-kind", choices=("individual", "company"))
        parser.add_argument("--root-folder-id", action="append", default=[])
        parser.add_argument("--file-id", action="append", default=[])
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expected-plan-sha256")
        try:
            result = asyncio.run(run(parser.parse_args()))
            sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            if any(item["status"] != "applied" for item in result.get("apply_results", [])):
                return 1
        except Exception as exc:
            safe_reasons = {
                "invalid_arguments",
                "configuration_missing",
                "schema_missing",
                "incomplete_inventory",
                "plan_changed",
                "sql_validation_failed",
            }
            reason = (
                str(exc)
                if isinstance(exc, RepairError) and str(exc) in safe_reasons
                else "operation_failed"
            )
            sys.stdout.write(json.dumps({"error": reason}) + "\n")
            return 1
        return 0

    finally:
        logging.disable(previous_level)


if __name__ == "__main__":
    sys.exit(main())

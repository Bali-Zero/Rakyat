"""Consume CRM passport jobs, or queue one explicitly selected historical document."""

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import suppress

import asyncpg

from backend.services.documents.passport_ocr_jobs import enqueue_selected
from backend.services.documents.passport_ocr_processing import process_one

logger = logging.getLogger(__name__)


async def run_passport_loop(database_url: str, stop: asyncio.Event) -> None:
    # A separate connection prevents long Drive and provider calls from holding the poller.
    async with asyncpg.create_pool(database_url, min_size=0, max_size=2) as pool:
        while not stop.is_set():
            try:
                result = await process_one(pool)
                if result["status"] != "not_claimed":
                    logger.info("CRM passport OCR outcome=%s", result["status"])
            except Exception as exc:
                # Client data and remote response bodies must stay out of logs.
                logger.warning("CRM passport OCR cycle failed (%s)", type(exc).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=30)


async def request_historical(document_id: int) -> int:
    """Repeatable operator entry point. The ordinary worker owns the claim and write."""
    async with asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=1) as pool:
        return await enqueue_selected(pool, [document_id])


async def import_local_receipt() -> str:
    from backend.services.documents.passport_ocr_receipt import AppleReceipt, apply_apple_receipt

    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        raise ValueError("oversized receipt")
    receipt = AppleReceipt.parse(json.loads(raw))
    async with asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=1) as pool:
        return await apply_apple_receipt(pool, receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--document-id", type=int)
    mode.add_argument(
        "--apple-vision-stdin",
        action="store_true",
        help="Import one local receipt from private stdin",
    )
    args = parser.parse_args()
    if args.apple_vision_stdin:
        from backend.services.documents.passport_ocr_receipt import ReceiptRejected

        try:
            status = asyncio.run(import_local_receipt())
        except ReceiptRejected as exc:
            sys.exit(f"receipt_refused={exc}")
        except Exception as exc:
            sys.exit(f"receipt_error={type(exc).__name__}")
        sys.stdout.write(f"receipt_status={status}\n")
    else:
        sys.stdout.write(f"new_jobs={asyncio.run(request_historical(args.document_id))}\n")

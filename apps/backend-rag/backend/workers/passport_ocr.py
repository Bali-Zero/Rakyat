"""Consume CRM passport jobs, or queue one explicitly selected historical document."""

import argparse
import asyncio
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", required=True, type=int)
    args = parser.parse_args()
    sys.stdout.write(f"new_jobs={asyncio.run(request_historical(args.document_id))}\n")

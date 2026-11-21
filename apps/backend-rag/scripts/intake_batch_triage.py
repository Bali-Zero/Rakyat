#!/usr/bin/env python3
"""Controlled intake backlog batch triage & gated auto-unblocker CLI (Tube 3).

Default mode (``--dry-run``) connects with ``default_transaction_read_only=on``
and never writes to Postgres or Drive. Output is strictly PII-free JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg

# Allow running directly from `apps/backend-rag` without pre-installed package.
_APP_ROOT = Path(__file__).resolve().parents[1]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from backend.services.intake import batch_triage, closed_taxonomy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only batch triage and 4-lock gated unblocker for intake_queue proposals."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Postgres DSN (defaults to DATABASE_URL env var).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run in strict Postgres read-only mode (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit VERDE rows (requires confirm-plan-sha256, max-apply, and env lock).",
    )
    parser.add_argument(
        "--confirm-plan-sha256",
        default=None,
        help="Exact plan_sha256 from --dry-run required for --apply.",
    )
    parser.add_argument(
        "--max-apply",
        type=int,
        default=None,
        help=f"Bounded cap of VERDE commits (1..{batch_triage.MAX_BATCH_APPLY}).",
    )
    parser.add_argument(
        "--committed-by",
        default="system:batch-triage",
        help="Audit actor recorded on --apply commits.",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--with-local-llm",
        action="store_true",
        help="Enable bounded Stage-B local Ollama qwen3.5:9b enum classification on unknown legible docs.",
    )
    parser.add_argument("--llm-limit", type=int, default=40)
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--ollama-model", default=closed_taxonomy.DEFAULT_OLLAMA_MODEL)
    parser.add_argument(
        "--prod-company-hashes-json",
        default=None,
        help="Optional path to JSON array of SHA-256 hashed normalized NIB/NPWP tokens for dry-run match simulation.",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional output path to write PII-free triage summary JSON.",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    dsn = (args.database_url or "").strip()
    if not dsn:
        print(json.dumps({"error": "DATABASE_URL is required"}))
        return 2

    prod_hashes: set[str] | None = None
    if args.prod_company_hashes_json:
        raw = json.loads(Path(args.prod_company_hashes_json).read_text(encoding="utf-8"))
        if isinstance(raw, list):
            prod_hashes = {str(h) for h in raw if isinstance(h, str) and len(h) == 64}

    if args.apply:
        if prod_hashes is not None:
            print(json.dumps({"error": "--prod-company-hashes-json is allowed in --dry-run only"}))
            return 2
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        try:
            result = await batch_triage.apply_triage_plan(
                pool,
                apply=True,
                confirm_plan_sha256=args.confirm_plan_sha256,
                max_apply=args.max_apply,
                committed_by=args.committed_by,
                batch_size=args.batch_size,
                limit=args.limit,
                with_local_llm=bool(args.with_local_llm),
                llm_limit=args.llm_limit,
                ollama_url=args.ollama_url,
                ollama_model=args.ollama_model,
                prod_company_hashes=prod_hashes,
            )
        finally:
            await pool.close()
        payload = {"mode": "apply", **result}
    else:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=2,
            server_settings={"default_transaction_read_only": "on"},
        )
        try:
            summary = await batch_triage.run_dry_run_triage(
                pool,
                batch_size=args.batch_size,
                limit=args.limit,
                with_local_llm=bool(args.with_local_llm),
                llm_limit=args.llm_limit,
                ollama_url=args.ollama_url,
                ollama_model=args.ollama_model,
                prod_company_hashes=prod_hashes,
            )
        finally:
            await pool.close()
        payload = {"mode": "dry_run", **summary.to_public_dict()}

    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output_json:
        Path(args.output_json).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())

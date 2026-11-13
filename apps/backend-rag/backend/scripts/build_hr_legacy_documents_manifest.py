"""R5 manifest builder (SPEC v2.1 R5, STAFF_CARD.md C13): READ-ONLY, run once on the production
machine through stdin, before the migration job exists there:

    fly ssh console -a nuzantara-rag -C 'python -' < backend/scripts/build_hr_legacy_documents_manifest.py

Prints ONE JSON document: for every present-but-unverified slot the tuple
`[hr_employees.id, slot, sha256(value)]` (`docs.contract` folded into `contract`: it is never a
slot of its own), and counts. Never a value, a name or a Drive id. Presence and verification come
from the shared projection (`project_documents`), the function every card reader and the job use.
The output, committed as `hr_legacy_documents_manifest.json`, is the job's input.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from collections import Counter

import asyncpg

from backend.app.services.hr.hr_card_models import DOCUMENT_SLOTS, _slot_value, project_documents

ID_SHAPE = re.compile(r"^[A-Za-z0-9_-]{25,100}$")
DOC_KEYS = frozenset(DOCUMENT_SLOTS) - {"contract"}


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch("SELECT id, profile::text AS p FROM hr_employees ORDER BY id")
    finally:
        await conn.close()
    entries: list[list[object]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        profile = json.loads(row["p"]) if row["p"] else None
        if not isinstance(profile, dict):
            continue
        for slot, state in project_documents(profile).items():
            if state["file_id"] is not None:
                counts["verified"] += 1
            elif state["present"]:
                value = _slot_value(profile, slot)
                entries.append([row["id"], slot, hashlib.sha256(value.encode()).hexdigest()])
                counts[f"present_unverified:{slot}"] += 1
                if ID_SHAPE.match(value) and re.search(r"[A-Za-z]", value):
                    counts["id_shaped"] += 1
        docs = profile.get("docs")
        if isinstance(docs, dict):
            counts["docs_unknown_keys"] += len(set(docs) - DOC_KEYS - {"contract"})
            if "contract" in docs:
                counts["docs.contract"] += 1
                if docs["contract"] == _slot_value(profile, "contract"):
                    counts["docs.contract_equal_to_contract.file_id"] += 1
    counts["entries"] = len(entries)
    json.dump(
        {
            "spec": "SPEC v2.1 R5 (Bali-Zero/Rakyat #270)",
            "entries": entries,
            "counts": dict(sorted(counts.items())),
        },
        sys.stdout,
        indent=1,
    )
    sys.stdout.write("\n")


asyncio.run(main())

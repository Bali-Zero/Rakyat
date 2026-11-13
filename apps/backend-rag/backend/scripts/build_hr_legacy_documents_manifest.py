"""R5 manifest builder (SPEC v2.1 R5, STAFF_CARD.md C13): READ-ONLY, run once on the production
machine through stdin, before the migration job exists there:

    fly ssh console -a nuzantara-rag -C 'python -' < backend/scripts/build_hr_legacy_documents_manifest.py

Prints ONE JSON document: for every present-but-unverified slot the tuple
`[hr_employees.id, slot, sha256(value)]` (`docs.contract` folded into `contract`: it is never a
slot of its own), and counts. Never a value, a name or a Drive id. Presence and verification come
from the shared projection (`project_documents`), the function every card reader and the job use.
The output, committed as `hr_legacy_documents_manifest.json`, is the job's input.

Streams are sealed at the file-descriptor level before any non-stdlib import: fd 1 and fd 2 point at
/dev/null and the document leaves on a private duplicate of stdout, so an import-time print, a
library or a traceback can never reach the terminal. Any failure prints only `{"failed": "builder_failed"}`.
"""

import os
import sys

sys.stdout.flush()
_OUT = os.dup(1)
_NULL = os.open(os.devnull, os.O_WRONLY)
os.dup2(_NULL, 1)
os.dup2(_NULL, 2)
os.close(_NULL)

import asyncio
import hashlib
import io
import json
import re
from collections import Counter

ID_SHAPE = re.compile(r"^[A-Za-z0-9_-]{25,100}$")


async def build(out: io.StringIO) -> None:
    import asyncpg

    from backend.app.services.hr.hr_card_models import (
        DOCUMENT_SLOTS,
        _slot_value,
        project_documents,
    )

    doc_keys = frozenset(DOCUMENT_SLOTS) - {"contract"}
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
            counts["docs_unknown_keys"] += len(set(docs) - doc_keys - {"contract"})
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
        out,
        indent=1,
    )
    out.write("\n")


try:
    buf = io.StringIO()
    asyncio.run(build(buf))
    data, code = buf.getvalue().encode(), 0
except BaseException:
    data, code = b'{"failed": "builder_failed"}\n', 1
while data:
    data = data[os.write(_OUT, data) :]
os._exit(code)

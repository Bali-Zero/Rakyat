"""Round-2 R5(c): ACCOUNTING_EMAIL has exactly one definition.

The single source is ``backend/services/invoicing/accounting.py``; the invoice
service, the unpaid-invoice notifier and the practice status listener import
that very object, and the literal address appears in no other module of
``backend/services/invoicing`` nor in ``practice_status_listener.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from backend.services.crm import practice_status_listener
from backend.services.invoicing import (
    accounting,
    invoice_generator,
    invoice_service,
    unpaid_invoice_notifier,
)

IMPORTERS = (
    invoice_service,
    unpaid_invoice_notifier,
    practice_status_listener,
    invoice_generator,
)


def test_all_consumers_expose_the_same_object() -> None:
    for module in IMPORTERS:
        assert module.ACCOUNTING_EMAIL is accounting.ACCOUNTING_EMAIL, module.__name__
        assert module.ACCOUNTING_EMAIL == accounting.ACCOUNTING_EMAIL


def test_literal_appears_in_no_other_module() -> None:
    literal = accounting.ACCOUNTING_EMAIL
    invoicing_dir = Path(inspect.getsourcefile(accounting)).parent
    candidates = sorted(invoicing_dir.glob("*.py"))
    listener_file = Path(inspect.getsourcefile(practice_status_listener))
    for path in [*candidates, listener_file]:
        if path.name == "accounting.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert literal not in source, f"{literal!r} literal found in {path}"

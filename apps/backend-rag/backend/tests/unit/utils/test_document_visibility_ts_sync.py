"""Cross-language drift tripwire for DOCUMENT_VISIBILITY_ROLES.

The desk (`apps/mouth/.../modals/document-visibility.ts`) hand-copies the
roles the backend lets flip a document's `client_visible`, so the toggle is
only OFFERED to people the API would admit. The 2026-11-09 job titles were
added to the backend and forgotten in that copy: three colleagues kept the
permission on the API and lost the button. This test compares the two as
sets, reading the TS source as text, so the next title cannot go missing on
one side silently.

The TS set is wider on purpose: it also lists the CRM-admin ROLES (the
backend admits those through `is_crm_admin`). So the contract is
`TS == DOCUMENT_VISIBILITY_ROLES | CRM_ADMIN_ROLES`, not equality with the
document set alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.utils.crm_utils import DOCUMENT_VISIBILITY_ROLES, is_crm_admin

_TS_PATH = (
    Path(__file__).resolve().parents[6]
    / "apps"
    / "mouth"
    / "src"
    / "app"
    / "(workspace)"
    / "clients"
    / "[id]"
    / "components"
    / "modals"
    / "document-visibility.ts"
)

_SET_RE = re.compile(
    r"const\s+DOCUMENT_VISIBILITY_ROLES[^=]*=\s*new\s+Set\(\s*\[(?P<items>.*?)\]\s*\)",
    re.DOTALL,
)
_STRING_RE = re.compile(r"""["']([^"']+)["']""")
_COMMENT_RE = re.compile(r"//[^\n]*")

# The role branch of `is_crm_admin` (crm_utils.py), probed through the
# function itself so this list cannot drift from the backend either.
_CRM_ADMIN_ROLE_CANDIDATES = ("admin", "board member", "ceo", "founder")


def _extract_ts_roles(source: str) -> set[str]:
    match = _SET_RE.search(source)
    if match is None:
        raise AssertionError(
            f"Could not find `const DOCUMENT_VISIBILITY_ROLES = new Set([...])` in {_TS_PATH}. "
            "Update the extraction pattern; do not delete or weaken this test."
        )
    items = _COMMENT_RE.sub("", match.group("items"))
    return set(_STRING_RE.findall(items))


def _backend_roles() -> set[str]:
    crm_admin_roles = {
        role for role in _CRM_ADMIN_ROLE_CANDIDATES if is_crm_admin({"email": "", "role": role})
    }
    return set(DOCUMENT_VISIBILITY_ROLES) | crm_admin_roles


class TestDocumentVisibilityMirror:
    def test_ts_mirror_equals_the_backend_roles(self) -> None:
        assert _TS_PATH.is_file(), f"expected TS file at {_TS_PATH}"
        ts_roles = _extract_ts_roles(_TS_PATH.read_text())
        assert ts_roles, "extracted an EMPTY set: the extraction is broken, not the SSOT"
        backend = _backend_roles()
        assert ts_roles == backend, (
            f"missing in TS: {sorted(backend - ts_roles)}; extra in TS: {sorted(ts_roles - backend)}"
        )

    @pytest.mark.parametrize("title", ["direktur", "tax supervisor", "tax responsible"])
    def test_the_2026_11_09_titles_are_on_both_sides(self, title: str) -> None:
        assert title in DOCUMENT_VISIBILITY_ROLES
        assert title in _extract_ts_roles(_TS_PATH.read_text())

    def test_extraction_catches_a_missing_title(self) -> None:
        """Guilt control: drop one title from the TS text and the comparison fails."""
        source = _TS_PATH.read_text().replace('"tax supervisor",', "", 1)
        assert _extract_ts_roles(source) != _backend_roles()

    def test_extraction_fails_loudly_when_the_declaration_is_gone(self) -> None:
        with pytest.raises(AssertionError):
            _extract_ts_roles("export const SOMETHING_ELSE = 1;")

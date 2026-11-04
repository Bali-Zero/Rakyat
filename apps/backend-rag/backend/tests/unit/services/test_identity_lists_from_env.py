"""The staff-name / drive-root / admin lists come from the environment, never from git."""

from __future__ import annotations

import importlib
import sys

from backend.app.utils import crm_utils


def test_crm_admin_set_has_no_extra_admins_without_env(monkeypatch):
    monkeypatch.delenv("CRM_EXTRA_ADMIN_EMAILS_JSON", raising=False)
    monkeypatch.setattr(crm_utils, "_settings_admin_emails", lambda: frozenset({"global@example.test"}))
    assert crm_utils._crm_admin_emails() == frozenset({"global@example.test"})


def test_crm_admin_set_adds_env_admins(monkeypatch):
    monkeypatch.setenv("CRM_EXTRA_ADMIN_EMAILS_JSON", '["Extra@Example.test"]')
    monkeypatch.setattr(crm_utils, "_settings_admin_emails", lambda: frozenset({"global@example.test"}))
    assert crm_utils._crm_admin_emails() == frozenset({"global@example.test", "extra@example.test"})


def test_staging_promoter_tokens_include_env_aliases(monkeypatch):
    monkeypatch.setenv("TEAM_NAME_ALIASES_JSON", '["Zzalias"]')
    from backend.services.wa_copilot import staging_promoter

    reloaded = importlib.reload(staging_promoter)
    try:
        assert "zzalias" in reloaded.TEAM_NAME_TOKENS
        assert "bali zero" in reloaded.TEAM_NAME_TOKENS
    finally:
        monkeypatch.delenv("TEAM_NAME_ALIASES_JSON", raising=False)
        importlib.reload(staging_promoter)


def test_identity_resolver_prefixes_include_env_aliases(monkeypatch):
    monkeypatch.setenv("TEAM_NAME_ALIASES_JSON", '["zzalias"]')
    from backend.services.wa_copilot import identity_resolver

    reloaded = importlib.reload(identity_resolver)
    try:
        assert "zzalias" in reloaded._OPERATOR_PREFIXES
    finally:
        monkeypatch.delenv("TEAM_NAME_ALIASES_JSON", raising=False)
        importlib.reload(identity_resolver)


def _load_intake_script():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[4] / "scripts" / "intake_drive_contact_autocreate.py"
    spec = importlib.util.spec_from_file_location("intake_drive_contact_autocreate_env", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["intake_drive_contact_autocreate_env"] = module
    spec.loader.exec_module(module)
    return module


def test_drive_root_allowlist_is_generic_without_env(monkeypatch):
    monkeypatch.delenv("INTAKE_DRIVE_ROOT_EXTRA_JSON", raising=False)
    allow = _load_intake_script().DRIVE_ROOT_ALLOWLIST
    assert "EXTEND VISA" in allow
    assert "Extra Root" not in allow


def test_drive_root_allowlist_adds_env_roots(monkeypatch):
    monkeypatch.setenv("INTAKE_DRIVE_ROOT_EXTRA_JSON", '["Extra Root"]')
    allow = _load_intake_script().DRIVE_ROOT_ALLOWLIST
    assert "Extra Root" in allow and "EXTEND VISA" in allow

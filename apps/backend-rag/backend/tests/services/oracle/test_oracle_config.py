from importlib import import_module
from types import SimpleNamespace

import pytest

from backend.services.oracle.oracle_config import OracleConfiguration

config_module = import_module("backend.services.oracle.oracle_config")


@pytest.fixture
def oracle_settings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    settings = SimpleNamespace(
        google_credentials_json='{"type":"service_account"}',
        database_url="postgresql://db",
        openai_api_key="openai-key",
    )
    monkeypatch.setattr(config_module, "settings", settings)
    return settings


def test_google_credentials_json_defaults_to_empty_json(
    oracle_settings: SimpleNamespace,
) -> None:
    oracle_settings.google_credentials_json = None

    assert OracleConfiguration().google_credentials_json == "{}"


def test_database_url_uses_placeholder_when_missing(oracle_settings: SimpleNamespace) -> None:
    oracle_settings.database_url = None

    assert OracleConfiguration().database_url == "postgresql://user:pass@localhost/db"


def test_openai_api_key_returns_empty_string_when_missing(
    oracle_settings: SimpleNamespace,
) -> None:
    oracle_settings.openai_api_key = None

    assert OracleConfiguration().openai_api_key == ""

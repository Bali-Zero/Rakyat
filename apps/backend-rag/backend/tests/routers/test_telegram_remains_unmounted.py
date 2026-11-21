"""Telegram is retired: the unmounted remains stay unmounted and /handoff never needs it."""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from backend.app.setup.router_manifest import all_router_names

_ROUTERS_DIR = Path(__file__).resolve().parents[2] / "app" / "routers"


def test_preview_router_is_not_registered_or_shipped() -> None:
    assert "preview" not in all_router_names()
    assert not (_ROUTERS_DIR / "preview.py").exists()


def test_messaging_identity_has_no_telegram_lookup_route() -> None:
    from backend.app.routers.messaging_identity import router

    paths = {route.path for route in router.routes}
    assert not any("lookup/telegram" in path for path in paths)
    assert any(path.endswith("/lookup/phone/{phone}") for path in paths)


@pytest.mark.asyncio
async def test_handoff_succeeds_without_importing_telegram(monkeypatch) -> None:
    from backend.app.routers import visa_oracle as mod

    for name in [m for m in sys.modules if "telegram_bot_service" in m]:
        monkeypatch.delitem(sys.modules, name)

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if "telegram" in name:
            raise AssertionError(f"/handoff imported {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    row = {
        "quiz_answers": json.dumps({"nationality": "US", "purpose": "work", "duration": "long"}),
        "recommended_visas": json.dumps(
            [{"visa_name": "Working KITAS", "price": "10.000.013 IDR", "score": 4.0}]
        ),
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    body = mod.HandoffRequest(
        session_id="s1",
        quiz_answers={"nationality": "US", "purpose": "work", "duration": "long"},
        recommended_visas=[],
        messages=[],
    )
    resp = await mod.handoff(Request({"type": "http", "headers": []}), body, db_pool=pool)

    assert resp.success is True
    assert resp.telegram_sent is False
    assert "KITAS" in resp.whatsapp_url
    assert not any("telegram_bot_service" in m for m in sys.modules)

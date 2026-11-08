"""Telegram was retired by the owner on 2026-11-07.

This is a source policy, not an environment switch: stale credentials and inherited
opt-ins must never re-enable a retired integration. Historical data is retained.
"""


def telegram_is_retired() -> bool:
    return True

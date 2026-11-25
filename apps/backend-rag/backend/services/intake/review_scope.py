"""Shared SQL scope for the document-intake review feed and login gate."""

from __future__ import annotations

import os

_DEFAULT_REVIEW_SOURCES = ("whatsapp",)


def review_source_allowlist() -> tuple[str, ...]:
    raw = os.getenv("INTAKE_REVIEW_SOURCES", "")
    parsed = tuple(source.strip().lower() for source in raw.split(",") if source.strip())
    return parsed or _DEFAULT_REVIEW_SOURCES

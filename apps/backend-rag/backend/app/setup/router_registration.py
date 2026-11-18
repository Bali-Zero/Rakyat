"""
Router Registration Module
Centralizes all router inclusion logic.

Which routers each process mounts, and in which order, is declared in
``router_manifest.py``; the three functions below only iterate it.

All router imports are lazy (inside _register) to speed up module load time.
This is critical for Fly.io health checks - the server must start listening within 60s.
"""

from fastapi import FastAPI


def _register(api: FastAPI, target: str) -> None:
    """Import and include, in order, every manifest router of one registration target.

    Imports are lazy so the light process never loads heavy modules.
    """
    from importlib import import_module

    from backend.app.setup.router_manifest import _get_api_v1_prefix, ordered_entries

    for entry in ordered_entries(target):
        enabled = entry.condition is None or entry.condition()
        if not enabled and not entry.import_when_disabled:
            continue
        module = import_module(entry.import_path or f"backend.app.routers.{entry.name}")
        if not enabled:
            continue
        router = getattr(module, entry.attr)
        if entry.prefix is None:
            api.include_router(router)
        else:
            prefix = _get_api_v1_prefix() if entry.prefix == "__API_V1__" else entry.prefix
            api.include_router(router, prefix=prefix)


def include_routers(api: FastAPI) -> None:
    """
    Include all API routers - Prime Standard modular structure.

    The monolith is the full union of the api and rag groups.

    Args:
        api: FastAPI application instance
    """
    _register(api, "full")


def include_light_routers(api: FastAPI) -> None:
    """
    Include only LIGHT routers (DB + auth only, no RAG/ML dependencies).

    Used by the light process split — starts fast, no torch/sentence-transformers.

    Args:
        api: FastAPI application instance
    """
    _register(api, "api")


def include_heavy_routers(api: FastAPI) -> None:
    """
    Include HEAVY routers (RAG/KG/AI — require SearchService + ZantaraAIClient).

    Also includes health + handlers so /health works on the RAG process for Fly.io checks.

    Args:
        api: FastAPI application instance
    """
    _register(api, "rag")

"""Source-bound native licensing scopes for independently verified KBLI records."""

import hashlib
import json
import os
import stat
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Literal

from pydantic import BaseModel, Field

from backend.services.kbli_licence_derivation import (
    TIER,
    Refusal,
    ShapeDefect,
    derive_licence_groups,
)

# SHA-256 pins of public regulatory data, verified against the November 2026 OSS evidence.
_DATASET_SHA256 = (
    "75d5b9f527f3dbb56ebe560725ae260bc2ba5f897090495aec5c4c16c2ca7580"  # pragma: allowlist secret
)
_NATIVE_ROWS_SHA256 = {
    "55106": "eda9f92d5f0fc28aa612f29a9e51967dcf1b08e6104ba99c65297591ba3fc14c",  # pragma: allowlist secret
    "55201": "f91730f158d9349962c9bb5be1654b55365cd0e04cdf9d7381a995ebdccc484c",  # pragma: allowlist secret
    "55203": "26d0209c36cd531dad9e58a65564956429e6cdd56e3a970a55aea9b05f45601c",  # pragma: allowlist secret
    "55209": "7e7304ecbe0531f9a3522494475c5818a0f82d15954f8e8371a50f150ed32909",  # pragma: allowlist secret
    "56101": "fe7c191cbb2393ba38e8a4d5f7ca613ab45cae81f6e3fadc5e1b0999f2150449",  # pragma: allowlist secret
    "56102": "a1e3eee1d40a6a769cc9f8e9ad668e8f4b1ecbb85c34df58a92993d3828c5e94",  # pragma: allowlist secret
    "56303": "a8d9ed6b61d000e481e0ae56f6cc5fc34f3359f788291d338ebfde9bf01fa967",  # pragma: allowlist secret
}
_DATASET_VERSION = "v11.0-L2-oss-risk-20260911"
_DATASET_SIZE = 38_651_062
_NATIVE_SOURCE = "OSS_RBA_resiko_2025"
_LOAD_LOCK = Lock()


class NativeScopeRow(BaseModel):
    row_ordinal: int
    scope_index: int
    scope_uraian: str
    scale: list[str]
    type: str = Field(
        description="Canonical licence label, derived from risk when perizinan is empty."
    )
    risk_level: str
    requirements: list[str]
    obligations: list[str]
    authorities: list[str] = Field(
        description="Authority names on this row. Project-specific authority parameters are not represented."
    )


class NativeScopeSource(BaseModel):
    dataset_sha256: str
    rows_sha256: str
    dataset_version: str
    kbli_vintage: Literal["2025"]
    source: str


class NativeScopeCoverage(BaseModel):
    scope_rows: list[NativeScopeRow] | None = None
    scope_rows_status: Literal["available", "verified_empty", "unavailable"] = "unavailable"
    scope_rows_source: NativeScopeSource | None = None


def _dataset_path() -> Path:
    # Dockerfile installs canonical under /app/source_documents. In a development
    # checkout use its tracked counterpart, never a stale ignored backend copy.
    backend_root = Path(__file__).resolve().parents[2]
    root = (
        backend_root.parent.parent / "data" if backend_root.parent.name == "apps" else backend_root
    )
    return root / "source_documents/KBLI_2025_FINAL_CLEAN.json"


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _project_row(code: str, ordinal: int, row: dict) -> NativeScopeRow:
    scope_index, scope_text = row["scope_index"], row["scope_uraian"]
    if (
        type(scope_index) is not int
        or scope_index < 0
        or not isinstance(scope_text, str)
        or not scope_text.strip()
    ):
        raise ShapeDefect("Missing native scope")
    if row["kategori_risiko"] not in TIER:
        raise ShapeDefect("Unknown native risk tier")
    for field in ("skala_usaha", "persyaratan", "kewajiban", "kewenangan", "perizinan"):
        values = row[field]
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise ShapeDefect(f"Invalid native {field}")
    if row.get("jangka_waktu") is not None and not isinstance(row["jangka_waktu"], str):
        raise ShapeDefect("Invalid native time value")
    (group,) = derive_licence_groups(code, [row])
    return NativeScopeRow(
        row_ordinal=ordinal,
        scope_index=scope_index,
        scope_uraian=scope_text,
        scale=group.skala_usaha,
        type=group.name,
        risk_level=group.kategori_risiko,
        requirements=row["persyaratan"],
        obligations=row["kewajiban"],
        authorities=row["kewenangan"],
    )


def _code_coverage(code: str, records: list[dict]) -> NativeScopeCoverage:
    if len(records) != 1:
        return NativeScopeCoverage()
    record = records[0]
    if (
        record.get("_l1_source") != "OSS_RBA_2025_id_version_fff4053d"
        or record.get("_l2_source") != _NATIVE_SOURCE
        or record.get("_l2_status") is not None
    ):
        return NativeScopeCoverage()
    rows = record.get("per_skala")
    if not isinstance(rows, list) or not rows:
        return NativeScopeCoverage()
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row_pin = _NATIVE_ROWS_SHA256[code]
    if hashlib.sha256(encoded.encode()).hexdigest() != row_pin:
        return NativeScopeCoverage()
    try:
        projected = [_project_row(code, ordinal, row) for ordinal, row in enumerate(rows)]
    except (KeyError, TypeError, ValueError, Refusal, ShapeDefect):
        return NativeScopeCoverage()
    return NativeScopeCoverage(
        scope_rows=projected,
        scope_rows_status="available",
        scope_rows_source=NativeScopeSource(
            dataset_sha256=_DATASET_SHA256,
            rows_sha256=row_pin,
            dataset_version=_DATASET_VERSION,
            kbli_vintage="2025",
            source=_NATIVE_SOURCE,
        ),
    )


@lru_cache(maxsize=2)
def _load_snapshot(path: Path, identity: tuple[int, ...]) -> dict[str, NativeScopeCoverage]:
    with path.open("rb") as source:
        if _identity(os.fstat(source.fileno())) != identity:
            raise OSError("Dataset changed during scope read")
        raw = source.read(_DATASET_SIZE + 1)
    if hashlib.sha256(raw).hexdigest() != _DATASET_SHA256:
        return {}
    dataset = json.loads(raw)
    if dataset["metadata"]["version"] != _DATASET_VERSION or not isinstance(dataset["data"], list):
        return {}
    # Cache a whole source identity, so requests for different codes share one parse.
    return {
        code: _code_coverage(
            code,
            [
                record
                for record in dataset["data"]
                if isinstance(record, dict) and record.get("kode_kbli_2025") == code
            ],
        )
        for code in _NATIVE_ROWS_SHA256
    }


def is_registered_native_scope(code: str) -> bool:
    return code in _NATIVE_ROWS_SHA256


def read_native_scope_coverage(code: str, path: Path | None = None) -> NativeScopeCoverage:
    """Return all native rows for an attested source or an explicit coverage gap."""
    if not is_registered_native_scope(code):
        return NativeScopeCoverage()
    path = path if path is not None else _dataset_path()
    try:
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != _DATASET_SIZE:
            return NativeScopeCoverage()
        identity = _identity(file_stat)
        # Concurrent cold requests share one parse of the 38 MB package.
        with _LOAD_LOCK:
            result = _load_snapshot(path, identity).get(code, NativeScopeCoverage())
        if _identity(path.stat()) == identity:
            # Keep the cached evidence independent of mutations by a caller.
            return result.model_copy(deep=True)
    except (OSError, KeyError, TypeError, ValueError, Refusal, ShapeDefect):
        pass
    return NativeScopeCoverage()

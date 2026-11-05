"""Pure licence derivation shared by the canonical KG writer and KBLI inspection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCALE_ORDER: tuple[str, ...] = ("Mikro", "Kecil", "Menengah", "Besar")
SCALE_SET: frozenset[str] = frozenset(SCALE_ORDER)

# §4.2 item 1 — the four risk tiers to the licence name the statute assigns.
# A row whose own `perizinan` is explicit always wins over this (see
# `licence_name`).
TIER: dict[str, str] = {
    "Rendah": "NIB",
    "Menengah Rendah": "NIB dan Sertifikat Standar",
    "Menengah Tinggi": "NIB dan Sertifikat Standar",
    "Tinggi": "NIB dan Izin",
}

# §4.2 item 2 — MR and MT share a licence name but not a legal effect
# (Pasal 131(2) vs 132(2),(6)); the distinction rides as data.
VERIFICATION_BY_TIER: dict[str, str] = {
    "Menengah Rendah": "self-declared",
    "Menengah Tinggi": "verified",
}


class Refusal(Exception):
    """A code this run will not act on; the reason is the message."""


class ShapeDefect(Exception):
    """A canonical row/field does not have one of the four normalised shapes."""


def normalise_field(value: Any) -> list:
    """§4.2: `None` → `[]`, a non-empty string → `[value]`, a list → itself.

    Never iterate a string as characters. An empty string normalises to `[]`
    (same as absent) rather than `[""]`. Anything else (int/float/dict/bool)
    is a `ShapeDefect` — a row this defect touches makes the whole code
    refuse (§4.2 item 3), never a guess.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return value
    raise ShapeDefect(f"not None/str/list: {value!r} ({type(value).__name__})")


def licence_name(row: dict) -> str:
    """§4.2 item 1: the row's own `perizinan` wins; otherwise the tier maps."""
    perizinan = normalise_field(row.get("perizinan"))
    if perizinan:
        return "/".join(dict.fromkeys(perizinan))
    tier = row.get("kategori_risiko")
    if tier not in TIER:
        raise ShapeDefect(f"unknown kategori_risiko: {tier!r}")
    return TIER[tier]


def validate_skala(value: Any) -> list[str]:
    """A row's `skala_usaha` must be a non-empty subset of the four scales."""
    skala = normalise_field(value)
    if not skala or not set(skala) <= SCALE_SET:
        raise ShapeDefect(f"skala_usaha not a non-empty subset of {SCALE_ORDER}: {value!r}")
    return skala


@dataclass
class LicenceGroup:
    name: str
    kategori_risiko: str
    skala_usaha: list[str]
    jangka_waktu: str
    kewajiban: list[str] = field(default_factory=list)
    persyaratan: list[str] = field(default_factory=list)
    kewenangan: list[str] = field(default_factory=list)
    scope_uraian: list[str] = field(default_factory=list)
    fiktif_positif: bool = False
    pp28_row_indexes: list[Any] = field(default_factory=list)
    sertifikat_standar_verification: str | None = None


def derive_licence_groups(code: str, rows: list[dict] | None) -> list[LicenceGroup]:
    """§4.2: group `per_skala` rows by `(name, kategori_risiko)`, pinned aggregation.

    A malformed row (unknown tier, `skala_usaha` not a non-empty subset of the
    four scales, or any other shape defect) makes the WHOLE code refuse — the
    posture of `plan_licensing_only` in `kbli_documents_cure.py`.
    """
    if not rows:
        raise Refusal(f"{code}: canonical per_skala is [] — not eligible for a Phase 1 build")
    grouped: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for idx, row in enumerate(rows):
        try:
            name = licence_name(row)
            skala = validate_skala(row.get("skala_usaha"))
        except ShapeDefect as exc:
            raise Refusal(f"{code}: malformed per_skala row {idx} — {exc}") from exc
        tier = row["kategori_risiko"]
        key = (name, tier)
        if key not in grouped:
            grouped[key] = {
                "skala": set(),
                "jangka": [],
                "jangka_seen": set(),
                "kewajiban": [],
                "kewajiban_seen": set(),
                "persyaratan": [],
                "persyaratan_seen": set(),
                "kewenangan": [],
                "kewenangan_seen": set(),
                "scope_uraian": [],
                "scope_uraian_seen": set(),
                "fiktif_positif": False,
                "pp28_row_indexes": [],
            }
            order.append(key)
        g = grouped[key]
        g["skala"].update(skala)
        jangka = (row.get("jangka_waktu") or "").strip()
        if jangka and jangka not in g["jangka_seen"]:
            g["jangka_seen"].add(jangka)
            g["jangka"].append(jangka)
        for field_name in ("kewajiban", "persyaratan", "kewenangan", "scope_uraian"):
            for v in normalise_field(row.get(field_name)):
                seen_key = f"{field_name}_seen"
                if v not in g[seen_key]:
                    g[seen_key].add(v)
                    g[field_name].append(v)
        g["fiktif_positif"] = g["fiktif_positif"] or bool(row.get("fiktif_positif"))
        g["pp28_row_indexes"].append(row.get("scope_index"))

    groups: list[LicenceGroup] = []
    for name, tier in order:
        g = grouped[(name, tier)]
        groups.append(
            LicenceGroup(
                name=name,
                kategori_risiko=tier,
                skala_usaha=[s for s in SCALE_ORDER if s in g["skala"]],
                jangka_waktu="/".join(g["jangka"]),
                kewajiban=g["kewajiban"],
                persyaratan=g["persyaratan"],
                kewenangan=g["kewenangan"],
                scope_uraian=g["scope_uraian"],
                fiktif_positif=g["fiktif_positif"],
                pp28_row_indexes=g["pp28_row_indexes"],
                sertifikat_standar_verification=VERIFICATION_BY_TIER.get(tier),
            ),
        )
    return groups

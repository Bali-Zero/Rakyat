"""Closed-set document taxonomy classifier and deterministic Drive projection.

Architecture (Jev-style anti-loop bounded classifier):
1. Fixed 41-code closed enum aligned 1-to-1 with active ``document_categories``
   rows, plus an explicit ``"unknown"`` abstain outcome.
2. Stage A (deterministic): separator-normalized word-boundary filename matching
   and multi-anchor Indonesian/English legal/immigration/tax OCR heuristics.
   Zero network calls, zero retries, microsecond latency.
3. Stage B (optional local single-shot LLM): one stateless HTTP POST to an
   owner-hosted local/Tailscale Ollama instance (``qwen3.5:9b``, ``think: false``)
   with a JSON Schema ``enum`` constraint over the 42 valid codes and a strict
   confidence floor. Never calls external cloud APIs (UU PDP / Law 2) and never
   enters a multi-turn loop.
4. Stage C (deterministic projection): maps the taxonomy code to canonical
   ``document_category`` + ``drive_taxonomy.folder_for_category`` + subfolder hint.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

_DRIVE_TAXONOMY_MOD: Any = None


def _folder_for_category(category: str | None, client_type: str | None = "individual") -> str:
    """Resolve top-level Drive folder without executing ``backend.services.crm.__init__``."""
    global _DRIVE_TAXONOMY_MOD
    loaded = sys.modules.get("backend.services.crm.drive_taxonomy")
    if loaded is not None:
        return str(loaded.folder_for_category(category, client_type))
    if _DRIVE_TAXONOMY_MOD is None:
        dt_path = Path(__file__).resolve().parents[1] / "crm" / "drive_taxonomy.py"
        spec = importlib.util.spec_from_file_location("_intake_drive_taxonomy_ssot", dt_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load drive_taxonomy from {dt_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _DRIVE_TAXONOMY_MOD = mod
    return str(_DRIVE_TAXONOMY_MOD.folder_for_category(category, client_type))

ABSTAIN_CODE = "unknown"

# Exact 41 active codes in `document_categories` (Pro/PROD canonical CRM book).
CANONICAL_DOCUMENT_CODES: tuple[str, ...] = (
    # Immigration (13)
    "passport",
    "visa_kunjungan",
    "voa",
    "e_visa",
    "itas",
    "itap",
    "itk",
    "rptka",
    "imta",
    "merp",
    "sponsor_letter",
    "domicile_letter",
    "stm",
    # Corporate / Legal (8)
    "nib",
    "akta_pendirian",
    "sk_kemenkumham",
    "npwp_company",
    "surat_domisili",
    "oss_certificate",
    "izin_usaha",
    "virtual_office",
    # Tax & Social Security (8)
    "npwp",
    "efin",
    "spt_tahunan",
    "e_spt",
    "lkpm",
    "bpjs_tk",
    "bpjs_kes",
    "skt",
    # Personal / Civil (8)
    "photo",
    "cv",
    "diploma",
    "marriage_cert",
    "birth_cert",
    "police_clearance",
    "bank_statement",
    "health_cert",
    # Commercial / General (4)
    "contract",
    "invoice",
    "receipt",
    "other",
)

VALID_TAXONOMY_CODES: frozenset[str] = frozenset((*CANONICAL_DOCUMENT_CODES, ABSTAIN_CODE))

# Preserves all 20 legacy intake `writer.DOCUMENT_CATEGORY_MAP` mappings (e.g.
# bank_statement/payment_receipt -> other -> 04_Archive for individual Drive
# parity) while completing `"itas": "immigration"` and every active code in
# `document_categories`.
COMPLETE_DOCUMENT_CATEGORY_MAP: dict[str, str] = {
    # Legacy intake DOC_TYPES (preserved verbatim for test/router parity)
    "passport": "immigration",
    "kitas": "immigration",
    "visa": "immigration",
    "itap": "immigration",
    "itk": "immigration",
    "ktp": "personal",
    "family_card": "family",
    "birth_certificate": "family",
    "marriage_certificate": "family",
    "payment_receipt": "other",
    "travel_ticket": "other",
    "bank_statement": "other",
    "medical_insurance": "other",
    "npwp": "tax",
    "skt": "tax",
    "nib": "pma",
    "oss": "pma",
    "akta_pendirian": "pma",
    "profil_perseroan": "pma",
    "sk_kemenkumham": "pma",
    # Gap fix + complete canonical 41-code coverage
    "itas": "immigration",
    "visa_kunjungan": "immigration",
    "voa": "immigration",
    "e_visa": "immigration",
    "rptka": "immigration",
    "imta": "immigration",
    "merp": "immigration",
    "sponsor_letter": "immigration",
    "domicile_letter": "immigration",
    "stm": "immigration",
    "npwp_company": "pma",
    "surat_domisili": "pma",
    "oss_certificate": "pma",
    "izin_usaha": "pma",
    "virtual_office": "pma",
    "efin": "tax",
    "spt_tahunan": "tax",
    "e_spt": "tax",
    "lkpm": "tax",
    "bpjs_tk": "tax",
    "bpjs_kes": "tax",
    "photo": "personal",
    "cv": "personal",
    "diploma": "personal",
    "marriage_cert": "personal",
    "birth_cert": "personal",
    "police_clearance": "personal",
    "health_cert": "personal",
    "contract": "other",
    "invoice": "other",
    "receipt": "other",
    "other": "other",
}

# Alias normalization between legacy classifier/CRM codes and canonical 41 codes.
CODE_ALIASES: dict[str, str] = {
    "kitas": "itas",
    "visa": "visa_kunjungan",
    "oss": "oss_certificate",
    "birth_certificate": "birth_cert",
    "marriage_certificate": "marriage_cert",
    "payment_receipt": "receipt",
    "medical_insurance": "health_cert",
    "profil_perseroan": "akta_pendirian",
    "ktp": "other",
    "family_card": "other",
    "travel_ticket": "other",
}

COMPANY_CODES: frozenset[str] = frozenset(
    {
        "nib",
        "akta_pendirian",
        "sk_kemenkumham",
        "npwp_company",
        "surat_domisili",
        "oss_certificate",
        "izin_usaha",
        "virtual_office",
        "lkpm",
        "e_spt",
        "profil_perseroan",
        "oss",
    }
)

CLOSED_TAXONOMY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "enum": [*CANONICAL_DOCUMENT_CODES, ABSTAIN_CODE]},
        "subject_kind": {"type": "string", "enum": ["person", "company", "unknown"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["code", "subject_kind", "confidence"],
    "additionalProperties": False,
}

DEFAULT_MIN_CONFIDENCE = 0.65
DEFAULT_LLM_MIN_CONFIDENCE = 0.60
DEFAULT_OLLAMA_MODEL = "qwen3.5:9b"

_SEP_RE = re.compile(r"[^a-z0-9]+")
_PUNCT_SEP_RE = re.compile(r"[\s_/\\|,;:()\[\]{}]+")
_CORPORATE_MARKER_RE = re.compile(
    r"(?<![a-z0-9])(pt|cv|perseroan\s+terbatas|badan|perusahaan|tbk)(?![a-z0-9])"
)
_CV_CORPORATE_CONFLICT_RE = re.compile(
    r"(?<![a-z0-9])(pt|perseroan\s+terbatas|badan|perusahaan|tbk|nib|akta|siup|tdp|npwp|kbli|direktur|komisaris|komanditer|maju|jaya|abadi|karya|mandiri|sentosa|sejahtera|perkasa|nusantara)(?![a-z0-9])"
)


@dataclass(frozen=True)
class TaxonomyDecision:
    """Result of bounded closed-set document classification."""

    code: str
    document_category: str | None
    individual_folder: str | None
    company_folder: str | None
    subfolder_hint: str | None
    subject_kind: str
    confidence: float
    via: str  # deterministic | local_llm | abstain
    abstained: bool
    matched_rule: str | None = None


def normalize_to_canonical_code(doc_type: str | None) -> str:
    """Map an intake/legacy code onto one of the 41 canonical codes or ``unknown``."""
    raw = (doc_type or "").strip().lower()
    if not raw:
        return ABSTAIN_CODE
    if raw in VALID_TAXONOMY_CODES:
        return raw
    return CODE_ALIASES.get(raw, ABSTAIN_CODE)


def category_for_code(code: str | None) -> str | None:
    """Resolve the CRM ``document_category`` for an intake or canonical code."""
    raw = (code or "").strip().lower()
    if not raw or raw == ABSTAIN_CODE:
        return None
    if raw in COMPLETE_DOCUMENT_CATEGORY_MAP:
        return COMPLETE_DOCUMENT_CATEGORY_MAP[raw]
    canonical = CODE_ALIASES.get(raw)
    if canonical:
        return COMPLETE_DOCUMENT_CATEGORY_MAP.get(canonical)
    return None


def project_drive_destination(
    code: str | None,
    document_category: str | None = None,
    *,
    target_kind: str = "individual",
) -> tuple[str, str | None]:
    """Deterministically project ``(top_folder, subfolder_hint)`` from code + category."""
    raw = (code or "").strip().lower()
    canonical = normalize_to_canonical_code(raw)
    effective_category = document_category or category_for_code(raw) or "other"
    folder = _folder_for_category(effective_category, target_kind)

    hint: str | None = None
    if target_kind == "individual":
        if canonical in {"itas", "itap", "itk", "e_visa", "voa", "visa_kunjungan"}:
            hint = "Actual Visa"
        elif raw == "profil_perseroan":
            hint = "Profile Perseroan"
        elif canonical == "akta_pendirian":
            hint = "AKTA"
        elif canonical in {"nib", "oss_certificate"}:
            hint = "NIB"
        elif canonical == "npwp_company":
            hint = "NPWP"
        elif canonical == "npwp":
            hint = "NPWP personal"
        elif canonical == "lkpm":
            hint = "LKPM reports"
        elif canonical in {"spt_tahunan", "e_spt"}:
            hint = "SPT company" if canonical == "e_spt" else "SPT personal"
    else:
        if raw == "profil_perseroan":
            hint = "Profile Perseroan"
        elif canonical == "akta_pendirian":
            hint = "AKTA"
        elif canonical in {"nib", "oss_certificate"}:
            hint = "NIB"
        elif canonical == "npwp_company":
            hint = "NPWP"
        elif canonical == "lkpm":
            hint = "LKPM reports"
        elif canonical in {"spt_tahunan", "e_spt"}:
            hint = "SPT company"
        elif canonical == "npwp":
            hint = "NPWP personal"
    return folder, hint


def _make_decision(
    *,
    code: str,
    subject_kind: str,
    confidence: float,
    via: str,
    matched_rule: str | None = None,
    raw_code_hint: str | None = None,
) -> TaxonomyDecision:
    if code == ABSTAIN_CODE or code not in CANONICAL_DOCUMENT_CODES:
        return TaxonomyDecision(
            code=ABSTAIN_CODE,
            document_category=None,
            individual_folder=None,
            company_folder=None,
            subfolder_hint=None,
            subject_kind="unknown",
            confidence=round(float(confidence), 4),
            via="abstain",
            abstained=True,
            matched_rule=matched_rule,
        )
    category = category_for_code(raw_code_hint or code) or "other"
    indiv_folder, indiv_hint = project_drive_destination(
        raw_code_hint or code, category, target_kind="individual"
    )
    comp_folder, comp_hint = project_drive_destination(
        raw_code_hint or code, category, target_kind="company"
    )
    resolved_subject = subject_kind
    if resolved_subject == "unknown":
        resolved_subject = "company" if code in COMPANY_CODES else "person"
    return TaxonomyDecision(
        code=code,
        document_category=category,
        individual_folder=indiv_folder,
        company_folder=comp_folder,
        subfolder_hint=comp_hint if resolved_subject == "company" else indiv_hint,
        subject_kind=resolved_subject,
        confidence=round(float(confidence), 4),
        via=via,
        abstained=False,
        matched_rule=matched_rule,
    )


@dataclass(frozen=True)
class _Rule:
    code: str
    patterns: tuple[re.Pattern[str], ...]
    score: float
    subject_kind: str = "person"
    require_filename: bool = False


def _wb(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])", re.IGNORECASE)


_RULES: tuple[_Rule, ...] = (
    # Strong multi-word / Indonesian official OCR headers (highest specificity)
    _Rule(
        "sk_kemenkumham",
        (
            _wb(r"keputusan\s+menteri\s+hukum(?:\s+dan\s+hak\s+asasi\s+manusia)?"),
            _wb(r"ahu[\-\s]*\d{5,}\.ah\.01"),
            _wb(r"sk\s+(?:kemenkumham|menkumham|menhumkam|pendirian|pengesahan)"),
            _wb(r"direktorat\s+jenderal\s+administrasi\s+hukum\s+umum"),
        ),
        0.92,
        "company",
    ),
    _Rule(
        "nib",
        (
            _wb(r"nomor\s+induk\s+berusaha"),
            _wb(r"lembaga\s+pengelola\s+dan\s+penyelenggara\s+oss"),
            _wb(r"perizinan\s+berusaha\s+berbasis\s+risiko"),
            _wb(r"nib"),
        ),
        0.90,
        "company",
    ),
    _Rule(
        "oss_certificate",
        (
            _wb(r"sertifikat\s+standar"),
            _wb(r"online\s+single\s+submission"),
            _wb(r"oss\s+(?:certificate|rba)"),
        ),
        0.86,
        "company",
    ),
    _Rule(
        "akta_pendirian",
        (
            _wb(r"akta\s+(?:pendirian|perubahan|pernyataan\s+keputusan)"),
            _wb(r"deed\s+of\s+establishment"),
            _wb(r"profil(?:e)?\s+perseroan"),
            _wb(r"salinan\s+akta"),
        ),
        0.89,
        "company",
    ),
    _Rule(
        "izin_usaha",
        (
            _wb(r"izin\s+usaha(?:\s+tetap|\s+jasa|\s+industri)?"),
            _wb(r"siup"),
            _wb(r"tdp"),
            _wb(r"tanda\s+daftar\s+perusahaan"),
        ),
        0.84,
        "company",
    ),
    _Rule(
        "virtual_office",
        (
            _wb(r"virtual\s+office"),
            _wb(r"kantor\s+virtual"),
        ),
        0.84,
        "company",
    ),
    _Rule(
        "surat_domisili",
        (
            _wb(r"surat\s+keterangan\s+domisili\s+(?:perusahaan|usaha|badan)"),
            _wb(r"domisili\s+perusahaan"),
        ),
        0.85,
        "company",
    ),
    _Rule(
        "lkpm",
        (
            _wb(r"laporan\s+kegiatan\s+penanaman\s+modal"),
            _wb(r"investment\s+activity\s+report"),
            _wb(r"lkpm"),
        ),
        0.90,
        "company",
    ),
    _Rule(
        "e_spt",
        (
            _wb(r"bukti\s+penerimaan\s+elektronik"),
            _wb(r"e[\-\s]?spt"),
            _wb(r"bpe\s+djp"),
        ),
        0.88,
        "company",
    ),
    _Rule(
        "spt_tahunan",
        (
            _wb(r"surat\s+pemberitahuan\s+tahunan"),
            _wb(r"formulir\s+177[01]"),
            _wb(r"spt\s+(?:tahunan|badan|pribadi|orang\s+pribadi)"),
            _wb(r"spt"),
        ),
        0.86,
        "unknown",
    ),
    _Rule(
        "efin",
        (
            _wb(r"electronic\s+filing\s+identification\s+number"),
            _wb(r"efin"),
        ),
        0.89,
        "person",
    ),
    _Rule(
        "skt",
        (
            _wb(r"surat\s+keterangan\s+terdaftar"),
            _wb(r"skt\s+pajak"),
        ),
        0.88,
        "unknown",
    ),
    _Rule(
        "bpjs_tk",
        (
            _wb(r"bpjs\s+ketenagakerjaan"),
            _wb(r"jamsostek"),
            _wb(r"jaminan\s+kecelakaan\s+kerja"),
            _wb(r"bpjs\s+tk"),
        ),
        0.88,
        "unknown",
    ),
    _Rule(
        "bpjs_kes",
        (
            _wb(r"bpjs\s+kesehatan"),
            _wb(r"kartu\s+indonesia\s+sehat"),
            _wb(r"jkn[\-\s]?kis"),
        ),
        0.88,
        "person",
    ),
    _Rule(
        "passport",
        (
            _wb(r"p<[a-z]{3}[a-z0-9<]{10,}"),
            _wb(r"paspor\s+republik\s+indonesia"),
            _wb(r"republic\s+of\s+indonesia.{0,40}(?:e[\-\s]?)?passport"),
            _wb(r"machine\s+readable"),
        ),
        0.93,
        "person",
    ),
    _Rule(
        "itas",
        (
            _wb(r"izin\s+tinggal\s+terbatas"),
            _wb(r"limited\s+stay\s+permit"),
            _wb(r"kitas"),
            _wb(r"e[\-\s]?itas"),
            _wb(r"itas"),
        ),
        0.91,
        "person",
    ),
    _Rule(
        "itap",
        (
            _wb(r"izin\s+tinggal\s+tetap"),
            _wb(r"permanent\s+stay\s+permit"),
            _wb(r"kitap"),
            _wb(r"itap"),
        ),
        0.91,
        "person",
    ),
    _Rule(
        "itk",
        (
            _wb(r"izin\s+tinggal\s+kunjungan"),
            _wb(r"visit\s+stay\s+permit"),
            _wb(r"itk"),
        ),
        0.89,
        "person",
    ),
    _Rule(
        "rptka",
        (
            _wb(r"rencana\s+penggunaan\s+tenaga\s+kerja\s+asing"),
            _wb(r"rptka"),
        ),
        0.91,
        "person",
    ),
    _Rule(
        "imta",
        (
            _wb(r"izin\s+memperkerjakan\s+tenaga\s+kerja\s+asing"),
            _wb(r"izin\s+mempekerjakan\s+tenaga\s+(?:kerja\s+)?asing"),
            _wb(r"notifikasi\s+tka"),
            _wb(r"imta"),
        ),
        0.89,
        "person",
    ),
    _Rule(
        "merp",
        (
            _wb(r"multiple\s+exit\s+re[\-\s]?entry"),
            _wb(r"izin\s+masuk\s+kembali"),
            _wb(r"merp"),
            _wb(r"imk"),
        ),
        0.86,
        "person",
    ),
    _Rule(
        "voa",
        (
            _wb(r"visa\s+on\s+arrival"),
            _wb(r"e[\-\s]?voa"),
            _wb(r"voa"),
        ),
        0.89,
        "person",
    ),
    _Rule(
        "e_visa",
        (
            _wb(r"electronic\s+visa"),
            _wb(r"e[\-\s]?visa"),
            _wb(r"evisa"),
        ),
        0.88,
        "person",
    ),
    _Rule(
        "visa_kunjungan",
        (
            _wb(r"visa\s+kunjungan"),
            _wb(r"b211[ab]?"),
            _wb(r"visit\s+visa"),
        ),
        0.85,
        "person",
    ),
    _Rule(
        "sponsor_letter",
        (
            _wb(r"surat\s+(?:penjamin|sponsor|permohonan\s+sponsor)"),
            _wb(r"(?:surat\s+)?jaminan\s+(?:dan\s+tanggung\s+jawab\s+)?sponsor"),
            _wb(r"sponsorship?\s+letter"),
        ),
        0.85,
        "person",
    ),
    _Rule(
        "domicile_letter",
        (
            _wb(r"surat\s+keterangan\s+tempat\s+tinggal"),
            _wb(r"surat\s+keterangan\s+domisili(?!\s+(?:perusahaan|usaha|badan))"),
            _wb(r"sktt"),
            _wb(r"domicile\s+letter"),
        ),
        0.84,
        "person",
    ),
    _Rule(
        "stm",
        (
            _wb(r"surat\s+tanda\s+melapor"),
            _wb(r"kepolisian.*melapor"),
            _wb(r"stm"),
        ),
        0.84,
        "person",
    ),
    _Rule(
        "police_clearance",
        (
            _wb(r"surat\s+keterangan\s+catatan\s+kepolisian"),
            _wb(r"police\s+clearance"),
            _wb(r"skck"),
            _wb(r"casellario\s+giudiziale"),
        ),
        0.90,
        "person",
    ),
    _Rule(
        "marriage_cert",
        (
            _wb(r"akta\s+perkawinan"),
            _wb(r"akta\s+nikah"),
            _wb(r"buku\s+nikah"),
            _wb(r"marriage\s+certificate"),
            _wb(r"certificato\s+di\s+matrimonio"),
        ),
        0.90,
        "person",
    ),
    _Rule(
        "birth_cert",
        (
            _wb(r"akta\s+kelahiran"),
            _wb(r"kutipan\s+akta\s+kelahiran"),
            _wb(r"birth\s+certificate"),
            _wb(r"certificato\s+di\s+nascita"),
        ),
        0.90,
        "person",
    ),
    _Rule(
        "diploma",
        (
            _wb(r"ijazah"),
            _wb(r"transkrip\s+nilai"),
            _wb(r"bachelor(?:'s|\s+s|s)?\s+degree"),
            _wb(r"master(?:'s|\s+s|s)?\s+degree"),
            _wb(r"diploma"),
        ),
        0.83,
        "person",
    ),
    _Rule(
        "cv",
        (
            _wb(r"curriculum\s+vitae"),
            _wb(r"work\s+experience"),
        ),
        0.84,
        "person",
    ),
    _Rule(
        "health_cert",
        (
            _wb(r"surat\s+keterangan\s+sehat"),
            _wb(r"medical\s+certificate"),
            _wb(r"health\s+certificate"),
            _wb(r"medical\s+insurance"),
        ),
        0.83,
        "person",
    ),
    _Rule(
        "bank_statement",
        (
            _wb(r"rekening\s+koran"),
            _wb(r"mutasi\s+rekening"),
            _wb(r"bank\s+statement"),
            _wb(r"account\s+statement"),
            _wb(r"estratto\s+conto"),
        ),
        0.86,
        "unknown",
    ),
    _Rule(
        "invoice",
        (
            _wb(r"faktur\s+pajak"),
            _wb(r"proforma\s+invoice"),
            _wb(r"tax\s+invoice"),
            _wb(r"invoice"),
            _wb(r"fattura"),
        ),
        0.84,
        "unknown",
    ),
    _Rule(
        "receipt",
        (
            _wb(r"kwitansi"),
            _wb(r"tanda\s+terima\s+pembayaran"),
            _wb(r"payment\s+receipt"),
            _wb(r"bukti\s+(?:pembayaran|transfer)"),
            _wb(r"receipt"),
        ),
        0.84,
        "unknown",
    ),
    _Rule(
        "contract",
        (
            _wb(r"perjanjian\s+(?:sewa|kerja|kerjasama|kontrak)"),
            _wb(r"(?:lease|rental|employment|service)\s+agreement"),
            _wb(r"memorandum\s+of\s+understanding"),
            _wb(r"contract"),
        ),
        0.82,
        "unknown",
    ),
    _Rule(
        "photo",
        (
            _wb(r"pas\s+foto"),
            _wb(r"photo"),
            _wb(r"foto"),
        ),
        0.74,
        "person",
        require_filename=True,
    ),
)

_NPWP_MARKER = _wb(r"nomor\s+pokok\s+wajib\s+pajak|npwp")
_PASSPORT_BARE_PATTERNS = (_wb(r"paspor"), _wb(r"passport"))
_CV_FILENAME_PATTERNS = (_wb(r"cv"), _wb(r"resume"))
_SPECIFIC_IMMIGRATION_CODES: frozenset[str] = frozenset(
    {
        "itas",
        "itap",
        "itk",
        "rptka",
        "imta",
        "merp",
        "voa",
        "e_visa",
        "visa_kunjungan",
        "sponsor_letter",
        "domicile_letter",
        "stm",
    }
)
_ALLOWED_LOCAL_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "pro", "mini", "air", "air-m5", "nuzantara", "mini-pro2"}
)
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_ollama_client: httpx.AsyncClient | None = None


def _normalize_haystack(text: str | None) -> str:
    return _SEP_RE.sub(" ", (text or "").lower()).strip()


def _normalize_punct_haystack(text: str | None) -> str:
    return _PUNCT_SEP_RE.sub(" ", (text or "").lower()).strip()


def _pattern_matches(pat: re.Pattern[str], clean_text: str, punct_text: str) -> bool:
    return bool(
        (clean_text and pat.search(clean_text))
        or (punct_text and pat.search(punct_text))
    )


def _get_ollama_client() -> httpx.AsyncClient:
    """Return a shared stateless Ollama client with ``trust_env=False`` (no proxy egress)."""
    global _ollama_client
    if _ollama_client is None or getattr(_ollama_client, "is_closed", False):
        _ollama_client = httpx.AsyncClient(timeout=30.0, trust_env=False)
    return _ollama_client


async def close_ollama_client() -> None:
    """Close and reset the shared local Ollama client cleanly."""
    global _ollama_client
    client = _ollama_client
    _ollama_client = None
    if client is not None and hasattr(client, "aclose"):
        await client.aclose()


def classify_taxonomy_deterministic(
    file_name: str | None,
    ocr_text: str | None,
    *,
    subject_kind: str | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> TaxonomyDecision:
    """Stage A closed-set classifier using deterministic anchors on filename + OCR."""
    stem = Path(file_name).stem if file_name else ""
    fn_clean = _normalize_haystack(stem)
    ocr_clean = _normalize_haystack(ocr_text)
    combined = f"{fn_clean} {ocr_clean}".strip()

    fn_punct = _normalize_punct_haystack(stem)
    ocr_punct = _normalize_punct_haystack(ocr_text)
    combined_punct = f"{fn_punct} {ocr_punct}".strip()

    if not combined and not combined_punct:
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=0.0,
            via="abstain",
        )

    is_corporate_context = (
        subject_kind == "company" or bool(_CORPORATE_MARKER_RE.search(combined))
    )

    scores: dict[str, float] = {}
    rules_hit: dict[str, str] = {}
    filename_codes: set[str] = set()

    for rule in _RULES:
        for idx, pat in enumerate(rule.patterns):
            matched = (
                _pattern_matches(pat, fn_clean, fn_punct)
                if rule.require_filename
                else _pattern_matches(pat, combined, combined_punct)
            )
            if matched:
                fn_hit = bool(fn_clean or fn_punct) and _pattern_matches(pat, fn_clean, fn_punct)
                if fn_hit:
                    filename_codes.add(rule.code)
                bonus = 0.04 if (not rule.require_filename and fn_hit) else 0.0
                # Earlier pattern index in a rule reflects higher specificity.
                specificity_adj = max(0.0, (len(rule.patterns) - 1 - idx) * 0.015)
                candidate_score = min(0.99, rule.score + bonus + specificity_adj)
                if candidate_score > scores.get(rule.code, 0.0):
                    scores[rule.code] = candidate_score
                    rules_hit[rule.code] = pat.pattern
                break

    # Weak bare "passport" / "paspor": allow filename match or header match ONLY when
    # no specific non-passport immigration document matched.
    fn_has_bare_passport = any(
        _pattern_matches(p, fn_clean, fn_punct) for p in _PASSPORT_BARE_PATTERNS
    )
    fn_has_specific_imm = bool(filename_codes & _SPECIFIC_IMMIGRATION_CODES)
    ocr_has_specific_imm = bool(set(scores) & _SPECIFIC_IMMIGRATION_CODES)
    ocr_header = ocr_clean[:160]
    if "passport" not in scores:
        if fn_has_bare_passport and not fn_has_specific_imm:
            scores["passport"] = 0.90
            rules_hit["passport"] = "bare_passport_filename"
        elif not ocr_has_specific_imm and any(p.search(ocr_header) for p in _PASSPORT_BARE_PATTERNS):
            scores["passport"] = 0.85
            rules_hit["passport"] = "bare_passport_ocr_header"

    # Weak filename "cv" / "resume": suppress when filename/text indicates a "CV <Company>".
    if (
        "cv" not in scores
        and fn_clean
        and any(p.search(fn_clean) for p in _CV_FILENAME_PATTERNS)
        and subject_kind != "company"
        and not _CV_CORPORATE_CONFLICT_RE.search(combined)
    ):
        scores["cv"] = 0.82
        rules_hit["cv"] = "cv_filename"

    if _NPWP_MARKER.search(combined):
        npwp_code = "npwp_company" if is_corporate_context else "npwp"
        npwp_score = 0.90 if "nomor pokok wajib pajak" in combined else 0.85
        if npwp_score > scores.get(npwp_code, 0.0):
            scores[npwp_code] = npwp_score
            rules_hit[npwp_code] = "npwp_marker"

    if not scores:
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=0.0,
            via="abstain",
        )

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_code, best_score = ranked[0]
    if best_score < min_confidence:
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=best_score,
            via="abstain",
            matched_rule=rules_hit.get(best_code),
        )

    if len(ranked) > 1 and (best_score - ranked[1][1]) < 0.025:
        second_code = ranked[1][0]
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=best_score,
            via="abstain",
            matched_rule=f"tie:{best_code}:{second_code}",
        )

    inferred_subject = subject_kind or (
        "company"
        if (
            best_code in COMPANY_CODES
            or (
                is_corporate_context
                and best_code in {"contract", "invoice", "receipt", "bank_statement"}
            )
        )
        else "person"
    )
    return _make_decision(
        code=best_code,
        subject_kind=inferred_subject,
        confidence=best_score,
        via="deterministic",
        matched_rule=rules_hit.get(best_code),
    )


def _is_allowed_local_host(host: str | None) -> bool:
    if not host:
        return False
    cleaned = host.strip().lower().rstrip(".")
    if cleaned in _ALLOWED_LOCAL_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return ip.version == 4 and ip in _TAILSCALE_CGNAT


def _is_allowed_local_ollama_url(raw_url: str | None) -> bool:
    if not raw_url or not raw_url.strip():
        return False
    parsed = urlparse(raw_url.strip())
    return parsed.scheme in {"http", "https"} and _is_allowed_local_host(parsed.hostname)


def _validate_local_ollama_url(raw_url: str | None) -> str:
    url = (
        raw_url
        or os.environ.get("INTAKE_LOCAL_OLLAMA_URL")
        or os.environ.get("OLLAMA_URL")
        or "http://127.0.0.1:11434"
    ).strip().rstrip("/")
    parsed = urlparse(url)
    if not _is_allowed_local_ollama_url(url):
        raise ValueError(
            f"Refusing non-local LLM endpoint for intake taxonomy (Law 2): {parsed.hostname!r}"
        )
    return url


def _bounded_ocr_excerpt(ocr_text: str | None) -> str:
    cleaned = " ".join((ocr_text or "").split())
    if len(cleaned) <= 3300:
        return cleaned
    return f"{cleaned[:2500]} ... {cleaned[-800:]}"


async def classify_taxonomy_closed_llm(
    file_name: str | None,
    ocr_text: str | None,
    *,
    ollama_url: str | None = None,
    model: str = DEFAULT_OLLAMA_MODEL,
    timeout_s: float = 30.0,
    min_confidence: float = DEFAULT_LLM_MIN_CONFIDENCE,
) -> TaxonomyDecision:
    """Stage B single-shot local Ollama enum classifier (think=false, zero loops)."""
    base_url = _validate_local_ollama_url(ollama_url)
    excerpt = _bounded_ocr_excerpt(ocr_text)
    if len(excerpt) < 20 and not (file_name or "").strip():
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=0.0,
            via="abstain",
        )

    safe_filename = Path(file_name).name if file_name else "unknown"
    prompt = (
        "Classify this Indonesian/Bali CRM document into ONE exact code from the JSON schema enum.\n"
        "If uncertain or unreadable, return code='unknown'.\n"
        f"Filename: {safe_filename}\n"
        f"OCR excerpt:\n{excerpt}"
    )
    body = {
        "model": model,
        "prompt": prompt,
        "think": False,
        "stream": False,
        "format": CLOSED_TAXONOMY_JSON_SCHEMA,
        "options": {"temperature": 0.0, "num_predict": 96},
    }

    try:
        client = _get_ollama_client()
        resp = await client.post(f"{base_url}/api/generate", json=body, timeout=timeout_s)
        resp.raise_for_status()
        outer = resp.json()
    except (httpx.HTTPError, ValueError):
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=0.0,
            via="abstain",
            matched_rule="llm_transport_error",
        )

    raw_response = None
    if isinstance(outer, dict):
        raw_response = outer.get("response")
        if not isinstance(raw_response, str):
            msg = outer.get("message")
            if isinstance(msg, dict):
                raw_response = msg.get("content")
    if not isinstance(raw_response, str) or not raw_response.strip():
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=0.0,
            via="abstain",
            matched_rule="llm_empty_response",
        )

    try:
        parsed = json.loads(raw_response)
    except ValueError:
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=0.0,
            via="abstain",
            matched_rule="llm_invalid_json",
        )

    if not isinstance(parsed, dict):
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=0.0,
            via="abstain",
            matched_rule="llm_non_object",
        )

    code = str(parsed.get("code") or ABSTAIN_CODE).strip()
    subject_kind = str(parsed.get("subject_kind") or "unknown").strip()
    if subject_kind not in {"person", "company", "unknown"}:
        subject_kind = "unknown"
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0

    if code not in CANONICAL_DOCUMENT_CODES or conf < min_confidence:
        return _make_decision(
            code=ABSTAIN_CODE,
            subject_kind="unknown",
            confidence=conf,
            via="abstain",
            matched_rule="llm_below_floor" if code in CANONICAL_DOCUMENT_CODES else "llm_abstain",
        )

    return _make_decision(
        code=code,
        subject_kind=subject_kind,
        confidence=conf,
        via="local_llm",
        matched_rule=f"ollama:{model}",
    )


async def classify_taxonomy(
    file_name: str | None,
    ocr_text: str | None,
    *,
    subject_kind: str | None = None,
    allow_local_llm: bool = False,
    ollama_url: str | None = None,
    model: str = DEFAULT_OLLAMA_MODEL,
    timeout_s: float = 30.0,
) -> TaxonomyDecision:
    """Two-stage Jev-style closed taxonomy classifier: deterministic first, local LLM fallback."""
    stage_a = classify_taxonomy_deterministic(
        file_name,
        ocr_text,
        subject_kind=subject_kind,
    )
    if not stage_a.abstained and stage_a.confidence >= 0.80:
        return stage_a

    if allow_local_llm and len((ocr_text or "").strip()) >= 40:
        stage_b = await classify_taxonomy_closed_llm(
            file_name,
            ocr_text,
            ollama_url=ollama_url,
            model=model,
            timeout_s=timeout_s,
        )
        if not stage_b.abstained:
            return stage_b

    return stage_a

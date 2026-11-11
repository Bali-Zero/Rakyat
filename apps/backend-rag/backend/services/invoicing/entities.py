"""
Invoice issuer entities and practice-type routing (DAI-124, 2026-11-10).

Each invoice is issued by one legal entity: the letterhead (name, NPWP,
address) and the bank block on the PDF come from that entity. The entity is
chosen from the practice type code (1 invoice = 1 practice = 1 code).

Rules:
  * Codes not listed here keep today's behaviour: CIMB / PT BAYU BALI NOL.
  * An entity with any required field empty is never used: the invoice falls
    back to CIMB and a warning is logged (fail-safe).
  * Invoices whose ``invoices`` row was created before
    ``ENTITY_ROUTING_EFFECTIVE_FROM`` (WITA calendar date) stay on CIMB even
    when regenerated, so a re-sent invoice never changes its bank account.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone

from backend.app.metrics import invoice_issuer_unmapped_total
from backend.app.utils.logging_utils import get_logger, sanitize_for_log

logger = get_logger(__name__)

WITA = timezone(timedelta(hours=8), name="WITA")

# DRAFT awaiting owner decision.
ENTITY_ROUTING_EFFECTIVE_FROM = date(2026, 11, 12)


@dataclass(frozen=True)
class InvoiceEntity:
    """One issuing entity: letterhead + bank details printed on the invoice."""

    key: str
    company_name: str
    tax_id: str | None  # NPWP number only; rendered as "NPWP: <tax_id>"
    address: str | None  # letterhead address; optional (CIMB has none today)
    bank_account_name: str | None
    bank_account_number: str | None
    bank_name: str | None
    bank_branch: str | None
    bank_swift: str | None

    # Fields that must be non-empty before the entity can issue an invoice.
    REQUIRED_FIELDS = (
        "key",
        "company_name",
        "tax_id",
        "bank_account_name",
        "bank_account_number",
        "bank_name",
        "bank_branch",
        "bank_swift",
    )

    def missing_fields(self) -> list[str]:
        return [
            f.name
            for f in fields(self)
            if f.name in self.REQUIRED_FIELDS and not (getattr(self, f.name) or "").strip()
        ]

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()


# ── Entities ────────────────────────────────────────────────────────────────

# Today's issuer, moved unchanged from InvoiceGenerator's class constants.
CIMB_BAYU_BALI_NOL = InvoiceEntity(
    key="cimb_bayu_bali_nol",
    company_name="PT BAYU BALI NOL",
    tax_id="1000000001239938",
    address=None,
    bank_account_name="PT BAYU BALI NOL",
    bank_account_number="800198149700",
    bank_name="CIMB NIAGA",
    bank_branch="JL MELATI NO 29, DANGIN PURI KANGIN, DENPASAR, BALI, 80236",
    bank_swift="BNIAIDJA",
)

# Letterhead name: Asya, 2026-11-10. NPWP + bank details from the finance
# document and NPWP card dated 2026-11-10.
MANDIRI_BALI_NOL_IMPRESARIAT = InvoiceEntity(
    key="mandiri_bali_nol_impresariat",
    company_name="PT. Bali Nol Impresariat",
    tax_id="0537 9883 1390 6000",
    address="Jl Raya Anyar No 26, Kerobokan, Kuta Utara, Kab. Badung, Bali",
    bank_account_name="Bali Nol Impresariat",
    bank_account_number="1750002424074",
    bank_name="Bank Mandiri",
    bank_branch="Jl. Gatot Subroto Barat - Kota Denpasar, Badung, Bali 80361",
    bank_swift="BMRIIDJAXXX",
)

# Letterhead name per Asya's document (the NPWP card reads "BAYU SANTERO");
# address = NPWP address. Details: same source as Mandiri.
BNI_BAYU_SANTERO = InvoiceEntity(
    key="bni_bayu_santero",
    company_name="CV. Bayu Santero",
    tax_id="0021 5256 6290 6000",
    address="Jl Raya Anyar No 26, Kerobokan, Kuta Utara, Kab. Badung, Bali",
    bank_account_name="CV. Bayu Santero",
    bank_account_number="0301613030",
    bank_name="BNI (Bank Negara Indonesia)",
    bank_branch="Jl. Raya Semer no 26, Kerobokan, Badung, Bali 80361",
    bank_swift="BNINIDJARNN",
)

DEFAULT_ENTITY_KEY = CIMB_BAYU_BALI_NOL.key

# Looked up at call time so tests can swap in synthetic complete entities.
ENTITIES: dict[str, InvoiceEntity] = {
    e.key: e for e in (CIMB_BAYU_BALI_NOL, MANDIRI_BALI_NOL_IMPRESARIAT, BNI_BAYU_SANTERO)
}

# ── Practice type code → entity ─────────────────────────────────────────────

PRACTICE_TYPE_ENTITY_KEYS: dict[str, str] = {
    **dict.fromkeys(
        (
            "kitas",
            "extension_kitas",
            "kitas_born",
            "kitas_working_onshore",
            "kitas_working_offshore",
            "kitas_working_extend",
            "kitas_freelance_onshore",
            "kitas_freelance_offshore",
            "kitas_remote_worker_onshore",
            "kitas_remote_worker_offshore",
            "kitas_remote_worker_extend",
            "kitas_retirement_onshore",
            "kitas_retirement_offshore",
            "kitas_retirement_extend",
            "kitas_investor_2yr_onshore",
            "kitas_investor_2yr_offshore",
            "kitas_investor_2yr_extend",
            "kitas_spouse_1yr_onshore",
            "kitas_spouse_1yr_offshore",
            "kitas_spouse_1yr_extend",
            "kitas_spouse_2yr_onshore",
            "kitas_spouse_2yr_offshore",
            "kitas_spouse_2yr_extend",
            "kitas_dependent_1yr_onshore",
            "kitas_dependent_1yr_offshore",
            "kitas_dependent_1yr_extend",
            "kitas_dependent_2yr_onshore",
            "kitas_dependent_2yr_offshore",
            "kitas_dependent_2yr_extend",
            "kitap_application",
            "kitap_investor_merp",
            "kitap_retirement_merp",
            "kitap_dependent_merp",
            "merp_1yr",
            "merp_2yr",
            "new_pt",
            "revision_pt",
            "company_pt_pma",
            "company_revision",
            "company_revision_akta",
            "company_dissolving",
            "company_virtual_office",
            "urgent_1_day",
            "urgent_2_days",
            "urgent_3_days",
            "tax",
            "tax_annual_company",
            "tax_annual_company_zero",
            "tax_annual_personal",
            "tax_annual_personal_addl",
            "tax_annual_pkg_a",
            "tax_annual_pkg_b",
            "tax_annual_pkg_c",
            "tax_annual_pkg_d",
            "tax_annual_reporting",
            "tax_bpjs_employee",
            "tax_bpjs_insurance",
            "tax_close_pma_company",
            "tax_efin_application",
            "tax_lkpm_yearly",
            "tax_monthly_basic_0_50",
            "tax_monthly_basic_50_100",
            "tax_monthly_basic_100_200",
            "tax_monthly_basic_200_plus",
            "tax_monthly_bundled_0_50",
            "tax_monthly_bundled_50_100",
            "tax_monthly_bundled_100_200",
            "tax_monthly_bundled_200_plus",
            "tax_npwp_personal_coretax",
            "tax_npwpd_registration",
            "tax_registration",
            "tax_update_data_coretax",
        ),
        CIMB_BAYU_BALI_NOL.key,
    ),
    **dict.fromkeys(
        (
            "visa_c1_tourism",
            "visa_c2_business",
            "visa_c7_music_art",
            "visa_c8_journalism",
            "visa_c10_speaker",
            "visa_c18_work_trial",
            "visa_c22_internship_60d",
            "visa_c22_internship_180d",
            "visa_d12_business_1yr",
            "visa_d12_business_2yr",
            "visa_d1_tourism_1yr",
            "visa_d1_tourism_2yr",
            "visa_d1_tourism_5yr",
            "visa_d2_business_1yr",
            "visa_d2_business_2yr",
            "visa_d2_business_5yr",
            "visa_voa",
            "visa_b1_voa",
            "other_epo",
            "other_erp",
            "other_reset_molina",
            "other_born_report",
            "other_lapor_lahir_under",
            "other_lapor_lahir_up",
            "other_create_user_rptka",
            "other_cancel_rptka",
            "other_cancel_rptka_imta_wl",
            "other_imta_rptka_only_12mo",
            "other_imta_rptka_only_6mo_or_less",
            "other_mutation_address",
            "other_mutation_address_2_kanim",
            "other_mutation_passport",
            "other_acc_mutation",
            "other_acc_mutation_passport",
            "other_passport_5yr",
            "other_passport_10yr",
            "other_epassport_5yr",
            "other_epassport_10yr",
            "other_skck",
            "other_sktt",
            "other_domicilie",
            "other_domicilie_sktt",
            "other_driving_license",
        ),
        MANDIRI_BALI_NOL_IMPRESARIAT.key,
    ),
    **dict.fromkeys(
        ("voa_extension", "ext_b1_voa", "ext_c1_tourism"),
        BNI_BAYU_SANTERO.key,
    ),
}

KNOWN_UNMAPPED_CODES = frozenset(
    {
        "accessories",
        "extension_visa",
        "kitas_application",
        "open_inquiry",
        "other_acc_boarding_pass",
        "other_cancel_wajib_lapor",
        "other_create_molina_express",
        "other_custom",
        "property_purchase",
        "pt_pma_setup",
        "tax_consulting",
        "visa",
        "visa_bridging",
    }
)

ROUTING_REVIEW_GROUPS = {
    "d1_d2": (
        "visa_d1_tourism_1yr",
        "visa_d1_tourism_2yr",
        "visa_d1_tourism_5yr",
        "visa_d2_business_1yr",
        "visa_d2_business_2yr",
        "visa_d2_business_5yr",
    ),
    "retired": (
        "urgent_1_day",
        "urgent_2_days",
        "urgent_3_days",
        "other_driving_license",
        "other_create_molina_express",
    ),
}


def _normalize_code(practice_type_code: str | None) -> str | None:
    if practice_type_code is None:
        return None
    if not isinstance(practice_type_code, str):
        raise TypeError("practice_type_code must be str or None")
    return practice_type_code.strip().lower()


def _assert_registry_keys() -> None:
    for registry_key, entity in ENTITIES.items():
        if registry_key != entity.key:
            raise ValueError("Invoice entity registry key does not match entity key")


def _default_entity() -> InvoiceEntity:
    _assert_registry_keys()
    default = ENTITIES.get(DEFAULT_ENTITY_KEY)
    if default is None or default.missing_fields():
        raise ValueError("Default invoice entity is incomplete")
    return default


def _unmapped_labels(code: str | None) -> tuple[str, str, str]:
    if code is None:
        return "__none__", "none", "__none__"
    if not code:
        return "__empty__", "empty", "__empty__"
    if code in KNOWN_UNMAPPED_CODES:
        return code, "known_unmapped", code
    return "__other__", "unknown", code


def entity_key_for_code(practice_type_code: str | None) -> str:
    """Return a mapped issuer key or the observable default fallback."""
    code = _normalize_code(practice_type_code)
    if code is not None and code in PRACTICE_TYPE_ENTITY_KEYS:
        return PRACTICE_TYPE_ENTITY_KEYS[code]

    metric_code, code_kind, log_code = _unmapped_labels(code)
    safe_code = sanitize_for_log(log_code, max_len=128)
    context = {
        "event": "invoice_issuer_unmapped",
        "practice_type_code": safe_code,
        "code_kind": code_kind,
        "fallback_entity_key": DEFAULT_ENTITY_KEY,
        "counter": "zantara_invoice_issuer_unmapped_total",
        "counter_increment": 1,
    }
    logger.warning(
        "invoice_issuer_unmapped practice_type_code=%s fallback_entity_key=%s",
        safe_code,
        DEFAULT_ENTITY_KEY,
        extra={"context": context},
    )
    invoice_issuer_unmapped_total.labels(metric_code, code_kind).inc()
    return DEFAULT_ENTITY_KEY


def resolve_invoice_entity(
    practice_type_code: str | None,
    routed_at: datetime,
) -> InvoiceEntity:
    """Resolve a complete issuer from an aware database routing instant."""
    _normalize_code(practice_type_code)
    if not isinstance(routed_at, datetime):
        raise TypeError("routed_at must be a datetime")
    if routed_at.tzinfo is None or routed_at.utcoffset() is None:
        raise ValueError("routed_at must be timezone-aware")

    key = entity_key_for_code(practice_type_code)
    default = _default_entity()
    if routed_at.astimezone(WITA).date() < ENTITY_ROUTING_EFFECTIVE_FROM:
        return default

    entity = ENTITIES.get(key)
    if entity is not None and not entity.missing_fields():
        return entity

    reason = "mapped_entity_missing" if entity is None else "mapped_entity_incomplete"
    missing_fields = [] if entity is None else entity.missing_fields()
    normalized_code = _normalize_code(practice_type_code)
    logger.error(
        "invoice_issuer_fallback_default practice_type_code=%s configured_entity_key=%s fallback_entity_key=%s",
        sanitize_for_log(normalized_code, max_len=128),
        key,
        default.key,
        extra={
            "context": {
                "event": "invoice_issuer_fallback_default",
                "reason": reason,
                "practice_type_code": sanitize_for_log(normalized_code, max_len=128),
                "configured_entity_key": key,
                "missing_fields": missing_fields,
                "fallback_entity_key": default.key,
            }
        },
    )
    return default

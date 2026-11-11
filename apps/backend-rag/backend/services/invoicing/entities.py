"""
Invoice issuer entities and practice-type routing (DAI-124, 2026-11-10).

Each invoice is issued by one legal entity: the letterhead (name, NPWP,
address) and the bank block on the PDF come from that entity. The entity is
chosen from the practice type code (1 invoice = 1 practice = 1 code).

Rules:
  * ``PRACTICE_TYPE_ENTITY_KEYS`` is the only routing table. A code that is
    not in it keeps today's behaviour (the default entity) but is loud: a
    warning and ``zantara_invoice_issuer_unmapped_total`` name the code.
  * ``ENTITY_ROUTING_EFFECTIVE_FROM`` is the activation switch: ``None`` keeps
    routing dark (every new invoice is issued by the default entity); a WITA
    calendar date is compared with the database instant the issuer is chosen at.
  * A mapped entity with a required field empty is never used for a NEW
    invoice: the default entity is used and an error is logged.
  * This module only chooses an issuer for a NEW invoice. An invoice that
    already exists keeps the issuer stored on its ``invoices`` row; the
    service reads it back and never re-routes (invoice_service.py).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone

from backend.app.metrics import invoice_issuer_unmapped_total
from backend.app.utils.logging_utils import get_logger, sanitize_for_log

logger = get_logger(__name__)

WITA = timezone(timedelta(hours=8), name="WITA")

# Activation switch, set with the owner at merge time. None = routing is dark.
ENTITY_ROUTING_EFFECTIVE_FROM: date | None = None


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
    # bank_branch, bank_swift and address are optional: an empty one is omitted
    # from the PDF.
    REQUIRED_FIELDS = (
        "key",
        "company_name",
        "tax_id",
        "bank_account_name",
        "bank_account_number",
        "bank_name",
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

# OWNER RULING 2026-11-11: all four issuers print this one address. The house
# number is 2. Earlier sources said 26 (the first version of this module, the
# historical invoices): that number is superseded, do not "correct" it back.
ISSUER_ADDRESS = "Jalan Raya Anyar No. 2, Kerobokan, Kuta Utara, Kab. Badung, Bali"

# Today's issuer, moved unchanged from InvoiceGenerator's class constants.
CIMB_BAYU_BALI_NOL = InvoiceEntity(
    key="cimb_bayu_bali_nol",
    company_name="PT BAYU BALI NOL",
    tax_id="1000000001239938",
    address=ISSUER_ADDRESS,
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
    address=ISSUER_ADDRESS,
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
    address=ISSUER_ADDRESS,
    bank_account_name="CV. Bayu Santero",
    bank_account_number="0301613030",
    bank_name="BNI (Bank Negara Indonesia)",
    bank_branch="Jl. Raya Semer no 26, Kerobokan, Badung, Bali 80361",
    bank_swift="BNINIDJARNN",
)

# Tax family issuer (owner 2026-11-11). NPWP from the company tax card in Drive;
# bank block as printed on the historical invoices. Branch and SWIFT are not
# printed on those invoices and are optional.
PERMATA_BALI_NOL_KONSULTAN = InvoiceEntity(
    key="permata_bali_nol_konsultan",
    company_name="PT Bali Nol Konsultan",
    tax_id="0509 8157 3490 6000",
    address=ISSUER_ADDRESS,
    bank_account_name="Bali Nol Konsultan",
    bank_account_number="0123-7845-699",
    bank_name="Bank Permata",
    bank_branch=None,
    bank_swift=None,
)

DEFAULT_ENTITY_KEY = CIMB_BAYU_BALI_NOL.key

# Looked up at call time so tests can swap in synthetic complete entities.
ENTITIES: dict[str, InvoiceEntity] = {
    e.key: e
    for e in (
        CIMB_BAYU_BALI_NOL,
        PERMATA_BALI_NOL_KONSULTAN,
        MANDIRI_BALI_NOL_IMPRESARIAT,
        BNI_BAYU_SANTERO,
    )
}

# ── Practice type code → entity ─────────────────────────────────────────────

PRACTICE_TYPE_ENTITY_KEYS: dict[str, str] = {
    # issuer 1, PT Bayu Bali Nol (default)
    "accessories": CIMB_BAYU_BALI_NOL.key,
    "ext_c1_tourism": CIMB_BAYU_BALI_NOL.key,
    "open_inquiry": CIMB_BAYU_BALI_NOL.key,
    "other_cancel_rptka": CIMB_BAYU_BALI_NOL.key,
    "other_cancel_wajib_lapor": CIMB_BAYU_BALI_NOL.key,
    "other_create_molina_express": CIMB_BAYU_BALI_NOL.key,
    "other_create_user_rptka": CIMB_BAYU_BALI_NOL.key,
    "other_driving_license": CIMB_BAYU_BALI_NOL.key,
    "other_epassport_10yr": CIMB_BAYU_BALI_NOL.key,
    "other_epassport_5yr": CIMB_BAYU_BALI_NOL.key,
    "other_epo": CIMB_BAYU_BALI_NOL.key,
    "other_erp": CIMB_BAYU_BALI_NOL.key,
    "other_imta_rptka_only_12mo": CIMB_BAYU_BALI_NOL.key,
    "other_imta_rptka_only_6mo_or_less": CIMB_BAYU_BALI_NOL.key,
    "other_mutation_address": CIMB_BAYU_BALI_NOL.key,
    "other_mutation_address_2_kanim": CIMB_BAYU_BALI_NOL.key,
    "other_mutation_passport": CIMB_BAYU_BALI_NOL.key,
    "other_passport_10yr": CIMB_BAYU_BALI_NOL.key,
    "other_passport_5yr": CIMB_BAYU_BALI_NOL.key,
    "other_reset_molina": CIMB_BAYU_BALI_NOL.key,
    "other_skck": CIMB_BAYU_BALI_NOL.key,
    "other_sktt": CIMB_BAYU_BALI_NOL.key,
    "property_purchase": CIMB_BAYU_BALI_NOL.key,
    "urgent_1_day": CIMB_BAYU_BALI_NOL.key,
    "urgent_2_days": CIMB_BAYU_BALI_NOL.key,
    "urgent_3_days": CIMB_BAYU_BALI_NOL.key,
    "visa": CIMB_BAYU_BALI_NOL.key,
    "visa_b1_voa": CIMB_BAYU_BALI_NOL.key,
    "visa_bridging": CIMB_BAYU_BALI_NOL.key,
    "visa_c10_speaker": CIMB_BAYU_BALI_NOL.key,
    "visa_c18_work_trial": CIMB_BAYU_BALI_NOL.key,
    "visa_c1_tourism": CIMB_BAYU_BALI_NOL.key,
    "visa_c22_internship_180d": CIMB_BAYU_BALI_NOL.key,
    "visa_c22_internship_60d": CIMB_BAYU_BALI_NOL.key,
    "visa_c2_business": CIMB_BAYU_BALI_NOL.key,
    "visa_c7_music_art": CIMB_BAYU_BALI_NOL.key,
    "visa_c8_journalism": CIMB_BAYU_BALI_NOL.key,
    "visa_d12_business_1yr": CIMB_BAYU_BALI_NOL.key,
    "visa_d12_business_2yr": CIMB_BAYU_BALI_NOL.key,
    "visa_d1_tourism_1yr": CIMB_BAYU_BALI_NOL.key,
    "visa_d1_tourism_2yr": CIMB_BAYU_BALI_NOL.key,
    "visa_d1_tourism_5yr": CIMB_BAYU_BALI_NOL.key,
    "visa_d2_business_1yr": CIMB_BAYU_BALI_NOL.key,
    "visa_d2_business_2yr": CIMB_BAYU_BALI_NOL.key,
    "visa_d2_business_5yr": CIMB_BAYU_BALI_NOL.key,
    # issuer 4, PT Bali Nol Impresariat
    "company_dissolving": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "company_pt_pma": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "company_revision": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "company_revision_akta": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "company_virtual_office": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "extension_kitas": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitap_application": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitap_dependent_merp": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitap_investor_merp": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitap_retirement_merp": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_application": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_born": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_dependent_1yr_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_dependent_1yr_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_dependent_1yr_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_dependent_2yr_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_dependent_2yr_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_dependent_2yr_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_freelance_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_freelance_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_investor_2yr_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_investor_2yr_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_investor_2yr_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_remote_worker_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_remote_worker_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_remote_worker_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_retirement_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_retirement_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_retirement_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_spouse_1yr_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_spouse_1yr_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_spouse_1yr_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_spouse_2yr_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_spouse_2yr_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_spouse_2yr_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_working_extend": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_working_offshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "kitas_working_onshore": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "new_pt": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "other_born_report": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "other_custom": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "other_lapor_lahir_under": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "other_lapor_lahir_up": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "pt_pma_setup": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "revision_pt": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    "tax_close_pma_company": MANDIRI_BALI_NOL_IMPRESARIAT.key,
    # issuer 2, PT Bali Nol Konsultan (tax family)
    "tax": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_company": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_company_zero": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_personal": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_personal_addl": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_pkg_a": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_pkg_b": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_pkg_c": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_pkg_d": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_annual_reporting": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_bpjs_employee": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_bpjs_insurance": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_consulting": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_efin_application": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_lkpm_yearly": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_basic_0_50": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_basic_100_200": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_basic_200_plus": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_basic_50_100": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_bundled_0_50": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_bundled_100_200": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_bundled_200_plus": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_monthly_bundled_50_100": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_npwp_personal_coretax": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_npwpd_registration": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_registration": PERMATA_BALI_NOL_KONSULTAN.key,
    "tax_update_data_coretax": PERMATA_BALI_NOL_KONSULTAN.key,
    # issuer 3, CV Bayu Santero
    "ext_b1_voa": BNI_BAYU_SANTERO.key,
    "extension_visa": BNI_BAYU_SANTERO.key,
}

KNOWN_UNMAPPED_CODES = frozenset({"other_acc_boarding_pass"})

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
    effective_from = ENTITY_ROUTING_EFFECTIVE_FROM
    if effective_from is None or routed_at.astimezone(WITA).date() < effective_from:
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

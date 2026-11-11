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

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone

from backend.app.utils.logging_utils import get_logger

logger = get_logger(__name__)

WITA = timezone(timedelta(hours=8))

# First WITA calendar day on which new invoices are routed by practice type.
# None = routing is disabled: every invoice is issued by the default entity. The
# owner sets the date explicitly once the mapping has been reviewed by accounting.
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
    REQUIRED_FIELDS = (
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

MANDIRI_CODES: frozenset[str] = frozenset(
    {
        # Visa Index C & D
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
        # Multiple Entry Visa: Index D (finance, 2026-11-10)
        "visa_d1_tourism_1yr",
        "visa_d1_tourism_2yr",
        "visa_d1_tourism_5yr",
        "visa_d2_business_1yr",
        "visa_d2_business_2yr",
        "visa_d2_business_5yr",
        # VOA (new)
        "visa_voa",
        "visa_b1_voa",
        # Other services
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
        # Passport and civil documents
        "other_passport_5yr",
        "other_passport_10yr",
        "other_epassport_5yr",
        "other_epassport_10yr",
        "other_skck",
        "other_sktt",
        "other_domicilie",
        "other_domicilie_sktt",
        "other_driving_license",
    }
)

# Extend VOA, Extend C1. No dedicated D12 extension code exists in the repo.
BNI_CODES: frozenset[str] = frozenset(
    {
        "voa_extension",
        "ext_b1_voa",
        "ext_c1_tourism",
    }
)

# Explicit so the CIMB set does not depend on the default.
CIMB_CODES: frozenset[str] = frozenset(
    {
        # KITAS / KITAP and extensions
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
        # Company
        "new_pt",
        "revision_pt",
        "company_pt_pma",
        "company_revision",
        "company_revision_akta",
        "company_dissolving",
        "company_virtual_office",
        # Urgent surcharges
        "urgent_1_day",
        "urgent_2_days",
        "urgent_3_days",
        # Tax: unchanged for now
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
    }
)

ENTITY_CODES: Mapping[str, frozenset[str]] = {
    MANDIRI_BALI_NOL_IMPRESARIAT.key: MANDIRI_CODES,
    BNI_BAYU_SANTERO.key: BNI_CODES,
    CIMB_BAYU_BALI_NOL.key: CIMB_CODES,
}


def _issue_date_wita(invoice_created_at: datetime | None, now: datetime | None) -> date:
    moment = invoice_created_at or now or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(WITA).date()


def entity_key_for_code(practice_type_code: str | None) -> str:
    """Entity key the code is mapped to; unknown or empty codes map to CIMB."""
    code = (practice_type_code or "").strip().lower()
    for key, codes in ENTITY_CODES.items():
        if code in codes:
            return key
    return DEFAULT_ENTITY_KEY


def resolve_invoice_entity(
    practice_type_code: str | None,
    invoice_created_at: datetime | None = None,
    *,
    now: datetime | None = None,
) -> InvoiceEntity:
    """Pick the issuing entity for one invoice.

    ``invoice_created_at`` is ``invoices.created_at`` of the existing row for
    the practice (regenerate); ``None`` means a new invoice, dated ``now``.
    """
    default = ENTITIES[DEFAULT_ENTITY_KEY]

    effective_from = ENTITY_ROUTING_EFFECTIVE_FROM
    if effective_from is None or _issue_date_wita(invoice_created_at, now) < effective_from:
        return default

    key = entity_key_for_code(practice_type_code)
    entity = ENTITIES.get(key, default)
    if entity.key == default.key:
        return entity

    missing = entity.missing_fields()
    if missing:
        logger.warning(
            "Invoice entity %s incomplete (missing: %s) for practice type %s; falling back to %s",
            entity.key,
            ", ".join(missing),
            practice_type_code,
            default.key,
        )
        return default
    return entity

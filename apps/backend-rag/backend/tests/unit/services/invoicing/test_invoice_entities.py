"""
Issuer-entity routing for invoices (DAI-124).

Real NPWP / bank numbers for Mandiri and BNI are not in the repo yet, so the
tests that need a complete entity swap in synthetic values (TEST-NPWP,
TEST-ACCOUNT). No client data: only practice type codes.
"""

import dataclasses
import io
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.entities import (
    BNI_BAYU_SANTERO,
    BNI_CODES,
    CIMB_BAYU_BALI_NOL,
    CIMB_CODES,
    ENTITY_ROUTING_EFFECTIVE_FROM,
    MANDIRI_BALI_NOL_IMPRESARIAT,
    MANDIRI_CODES,
    WITA,
    resolve_invoice_entity,
)

AFTER_GO_LIVE = datetime(2026, 11, 20, 10, 0, tzinfo=WITA)


def _complete(entity, tag):
    return dataclasses.replace(
        entity,
        tax_id=f"TEST-NPWP-{tag}",
        bank_account_name=f"TEST-HOLDER-{tag}",
        bank_account_number=f"TEST-ACCOUNT-{tag}",
        bank_branch=f"TEST-BRANCH-{tag}",
        bank_swift=f"TEST-SWIFT-{tag}",
    )


@pytest.fixture
def filled_entities(monkeypatch):
    """Mandiri + BNI with synthetic, complete details."""
    mandiri = _complete(MANDIRI_BALI_NOL_IMPRESARIAT, "MANDIRI")
    bni = _complete(BNI_BAYU_SANTERO, "BNI")
    monkeypatch.setitem(entities.ENTITIES, mandiri.key, mandiri)
    monkeypatch.setitem(entities.ENTITIES, bni.key, bni)
    return mandiri, bni


# ── Routing ─────────────────────────────────────────────────────────────────


def test_visa_c1_routes_to_mandiri(filled_entities):
    mandiri, _ = filled_entities
    assert resolve_invoice_entity("visa_c1_tourism", now=AFTER_GO_LIVE) == mandiri


def test_voa_extension_routes_to_bni(filled_entities):
    _, bni = filled_entities
    assert resolve_invoice_entity("voa_extension", now=AFTER_GO_LIVE) == bni


@pytest.mark.parametrize(
    "code",
    ["kitas_working_onshore", "tax_annual_personal", "not_a_real_code", "", None],
)
def test_cimb_codes_and_unknown_codes_stay_on_cimb(filled_entities, code):
    assert resolve_invoice_entity(code, now=AFTER_GO_LIVE) == CIMB_BAYU_BALI_NOL


@pytest.mark.parametrize(
    "code",
    [
        "visa",
        "visa_bridging",
        "extension_visa",
        "other_acc_boarding_pass",
        "other_create_molina_express",
        "other_cancel_wajib_lapor",
        "accessories",
        "open_inquiry",
        "other_custom",
    ],
)
def test_undecided_codes_default_to_cimb(filled_entities, code):
    assert resolve_invoice_entity(code, now=AFTER_GO_LIVE) == CIMB_BAYU_BALI_NOL


def test_code_sets_are_disjoint():
    assert not (MANDIRI_CODES & BNI_CODES)
    assert not (MANDIRI_CODES & CIMB_CODES)
    assert not (BNI_CODES & CIMB_CODES)


# ── Fail-safe: incomplete entity → CIMB + warning ───────────────────────────


@pytest.mark.parametrize(
    ("code", "entity"),
    [("visa_c1_tourism", MANDIRI_BALI_NOL_IMPRESARIAT), ("voa_extension", BNI_BAYU_SANTERO)],
)
def test_incomplete_entity_falls_back_to_cimb_with_warning(monkeypatch, code, entity):
    # Synthetic incomplete copy: no NPWP and no account number.
    incomplete = dataclasses.replace(entity, tax_id=None, bank_account_number="")
    assert incomplete.missing_fields() == ["tax_id", "bank_account_number"]
    monkeypatch.setitem(entities.ENTITIES, entity.key, incomplete)
    fake_logger = MagicMock()
    monkeypatch.setattr(entities, "logger", fake_logger)

    assert resolve_invoice_entity(code, now=AFTER_GO_LIVE) == CIMB_BAYU_BALI_NOL
    fake_logger.warning.assert_called_once()
    assert entity.key in fake_logger.warning.call_args.args


def test_single_missing_field_is_enough_to_fall_back(filled_entities, monkeypatch):
    mandiri, _ = filled_entities
    monkeypatch.setitem(
        entities.ENTITIES, mandiri.key, dataclasses.replace(mandiri, bank_swift="  ")
    )
    assert resolve_invoice_entity("visa_c1_tourism", now=AFTER_GO_LIVE) == CIMB_BAYU_BALI_NOL


def test_cimb_entity_is_complete():
    assert CIMB_BAYU_BALI_NOL.is_complete


# ── Effective date ──────────────────────────────────────────────────────────


def test_invoice_created_before_go_live_stays_on_cimb(filled_entities):
    created = datetime(2026, 11, 11, 15, 0, tzinfo=WITA)
    assert (
        resolve_invoice_entity("visa_c1_tourism", created, now=AFTER_GO_LIVE) == CIMB_BAYU_BALI_NOL
    )


def test_go_live_boundary_is_wita_midnight(filled_entities):
    mandiri, _ = filled_entities
    midnight_wita = datetime(2026, 11, 12, 0, 0, tzinfo=WITA)
    just_before = midnight_wita - timedelta(seconds=1)
    assert ENTITY_ROUTING_EFFECTIVE_FROM == midnight_wita.date()
    assert (
        resolve_invoice_entity("visa_c1_tourism", midnight_wita.astimezone(timezone.utc)) == mandiri
    )
    assert (
        resolve_invoice_entity("visa_c1_tourism", just_before.astimezone(timezone.utc))
        == CIMB_BAYU_BALI_NOL
    )


def test_new_invoice_before_go_live_stays_on_cimb(filled_entities):
    now = datetime(2026, 11, 10, 12, 0, tzinfo=WITA)
    assert resolve_invoice_entity("visa_c1_tourism", None, now=now) == CIMB_BAYU_BALI_NOL


# ── Service wiring ──────────────────────────────────────────────────────────


def _service_with_conn(conn):
    from backend.services.invoicing.invoice_service import InvoiceAutomationService

    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    with (
        patch("backend.services.invoicing.invoice_service.ServiceAccountDriveService"),
        patch("backend.services.invoicing.invoice_service.InvoiceGenerator"),
    ):
        return InvoiceAutomationService(pool)


@pytest.mark.asyncio
async def test_regenerate_keeps_existing_invoice_date(filled_entities):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc))
    service = _service_with_conn(conn)

    issuer = await service._resolve_issuer(7, "visa_c1_tourism")

    assert issuer == CIMB_BAYU_BALI_NOL
    sql = conn.fetchval.call_args.args[0]
    assert "created_at" in sql and "invoices" in sql


@pytest.mark.asyncio
async def test_new_invoice_after_go_live_uses_mapped_entity(filled_entities):
    mandiri, _ = filled_entities
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    service = _service_with_conn(conn)

    with patch.object(entities, "datetime", wraps=datetime) as fake_dt:
        fake_dt.now.return_value = AFTER_GO_LIVE
        issuer = await service._resolve_issuer(7, "visa_c1_tourism")

    assert issuer == mandiri


@pytest.mark.asyncio
async def test_created_at_lookup_error_falls_back_to_cimb(filled_entities):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    service = _service_with_conn(conn)

    assert await service._resolve_issuer(7, "visa_c1_tourism") == CIMB_BAYU_BALI_NOL


# ── PDF rendering ───────────────────────────────────────────────────────────


def _pdf_text(pdf_bytes: bytes) -> str:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def _render(entity):
    from backend.services.invoicing.invoice_generator import _REPORTLAB_AVAILABLE, InvoiceGenerator

    if not _REPORTLAB_AVAILABLE:
        pytest.skip("reportlab not installed")
    return InvoiceGenerator().generate(
        practice_id=1,
        client_name="Synthetic Client",
        client_email=None,
        client_phone=None,
        client_address=None,
        practice_type="visa_c1_tourism",
        practice_description="C1 Tourism Visa",
        quoted_price=1_000_000,
        entity=entity,
    )


def test_pdf_letterhead_and_bank_block_use_selected_entity(filled_entities):
    mandiri, _ = filled_entities
    text = _pdf_text(_render(mandiri))

    assert "PT. Bali Nol Impresariat" in text
    assert "Jl Raya Anyar No 26" in text
    assert "NPWP: TEST-NPWP-MANDIRI" in text
    assert "TEST-ACCOUNT-MANDIRI" in text
    assert "TEST-SWIFT-MANDIRI" in text
    assert "MANDIRI" in text
    # Nothing from the CIMB issuer leaks into a Mandiri invoice.
    assert CIMB_BAYU_BALI_NOL.bank_account_number not in text
    assert CIMB_BAYU_BALI_NOL.tax_id not in text
    assert "CIMB NIAGA" not in text


def test_pdf_default_issuer_is_unchanged_cimb():
    text = _pdf_text(_render(None))

    assert CIMB_BAYU_BALI_NOL.company_name in text
    assert f"NPWP: {CIMB_BAYU_BALI_NOL.tax_id}" in text
    assert CIMB_BAYU_BALI_NOL.bank_account_number in text
    assert "CIMB NIAGA" in text


# ── Real issuer details (finance document + NPWP cards, 2026-11-10) ────────


def test_mandiri_details_exact():
    e = MANDIRI_BALI_NOL_IMPRESARIAT
    assert e.company_name == "PT. Bali Nol Impresariat"
    assert e.address == "Jl Raya Anyar No 26, Kerobokan, Kuta Utara, Kab. Badung, Bali"
    assert e.tax_id == "0537 9883 1390 6000"
    assert e.bank_name == "Bank Mandiri"
    assert e.bank_account_name == "Bali Nol Impresariat"
    assert e.bank_account_number == "1750002424074"
    assert e.bank_branch == "Jl. Gatot Subroto Barat - Kota Denpasar, Badung, Bali 80361"
    assert e.bank_swift == "BMRIIDJAXXX"
    assert e.is_complete


def test_bni_details_exact():
    e = BNI_BAYU_SANTERO
    assert e.company_name == "CV. Bayu Santero"
    assert e.address == "Jl Raya Anyar No 26, Kerobokan, Kuta Utara, Kab. Badung, Bali"
    assert e.tax_id == "0021 5256 6290 6000"
    assert e.bank_name == "BNI (Bank Negara Indonesia)"
    assert e.bank_account_name == "CV. Bayu Santero"
    assert e.bank_account_number == "0301613030"
    assert e.bank_branch == "Jl. Raya Semer no 26, Kerobokan, Badung, Bali 80361"
    assert e.bank_swift == "BNINIDJARNN"
    assert e.is_complete


def test_cimb_details_unchanged():
    e = CIMB_BAYU_BALI_NOL
    assert e.company_name == "PT BAYU BALI NOL"
    assert e.tax_id == "1000000001239938"
    assert e.bank_account_number == "800198149700"
    assert e.bank_name == "CIMB NIAGA"
    assert e.bank_swift == "BNIAIDJA"


@pytest.mark.parametrize(
    "code",
    [
        "visa_d1_tourism_1yr",
        "visa_d1_tourism_2yr",
        "visa_d1_tourism_5yr",
        "visa_d2_business_1yr",
        "visa_d2_business_2yr",
        "visa_d2_business_5yr",
    ],
)
def test_index_d_multiple_entry_routes_to_real_mandiri(code):
    assert resolve_invoice_entity(code, now=AFTER_GO_LIVE) == MANDIRI_BALI_NOL_IMPRESARIAT


@pytest.mark.parametrize(
    ("code", "present", "absent"),
    [
        ("visa_c1_tourism", "1750002424074", "800198149700"),
        ("voa_extension", "0301613030", "800198149700"),
        ("kitas_working_onshore", "800198149700", "1750002424074"),
    ],
)
def test_pdf_with_real_routing_shows_expected_account(code, present, absent):
    entity = resolve_invoice_entity(code, now=AFTER_GO_LIVE)
    text = _pdf_text(_render(entity))
    assert present in text
    assert absent not in text

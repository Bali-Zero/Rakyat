"""Synthetic tests for the client "needs attention" builder (owner rules 2026-11-03).

Every rule has a guilt case (it fires) and an innocence case (it stays quiet).
No real client data: names and ids are placeholders.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.services.crm.client_attention import build_client_attention, classify_immigration

TODAY = date(2026, 11, 3)


def d(offset: int, base: date = TODAY) -> date:
    return base + timedelta(days=offset)


def doc(doc_id: int, doc_type: str, expiry: date | None, **kw: Any) -> dict[str, Any]:
    row = {
        "id": doc_id,
        "document_type": doc_type,
        "status": "active",
        "expiry_date": expiry,
        "issue_date": None,
        "fiscal_year": None,
        "family_member_id": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "deleted_at": None,
        "is_archived": False,
        "ocr_extracted_data": None,
    }
    row.update(kw)
    return row


def ocr(visa_type: str) -> dict[str, Any]:
    return {"raw_response": {"visa_type": visa_type}}


def run(
    documents: list[dict[str, Any]] | None = None,
    *,
    today: date = TODAY,
    client: dict[str, Any] | None = None,
    family: list[dict[str, Any]] | None = None,
    companies: list[dict[str, Any]] | None = None,
    lkpm: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return build_client_attention(
        client=client or {"id": 1, "full_name": "Client A"},
        family_members=family or [],
        documents=documents or [],
        company_links=companies or [],
        lkpm_reports=lkpm or [],
        today=today,
    )


def of_kind(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [i for i in result["attention"] if i["kind"] == kind]


# ── classifier ──────────────────────────────────────────────────────────────


def test_classifier_buckets() -> None:
    assert classify_immigration("kitas", None)[:2] == ("stay", "KITAS")
    assert classify_immigration("itap", None)[:2] == ("stay", "KITAP")
    assert classify_immigration("visa", "Visa Tinggal Terbatas (E28A)")[1:] == ("KITAS", "E28A")
    assert classify_immigration("visa", "VOA")[0] == "visit"
    assert classify_immigration("visa", "B211A Visit Visa")[0] == "visit"
    assert classify_immigration("visa", "D12 Pre-Investment")[0] == "visit"
    assert classify_immigration("e_visa", None)[0] == "visit"
    assert classify_immigration("itk", None)[0] == "itk"
    assert classify_immigration("visa", "VISIT STAY PERMIT")[0] == "itk"
    assert classify_immigration("epo", None)[0] == "epo"


def test_classifier_innocence_non_immigration_and_unknown() -> None:
    assert classify_immigration("passport", None) is None
    assert classify_immigration("misc_document", None) is None
    # A specific non-permit type must not borrow a permit from its OCR text.
    assert classify_immigration("rptka", "KITAS E28A") is None


# ── stay permits and supersession ───────────────────────────────────────────


def test_kitas_90_day_boundary() -> None:
    urgent = run([doc(1, "kitas", d(90))])["attention"]
    assert [(i["label"], i["state"], i["days"]) for i in urgent] == [("KITAS", "urgent", 90)]
    assert run([doc(1, "kitas", d(91))])["attention"] == []
    expired = run([doc(1, "kitas", d(0))])["attention"]
    assert expired[0]["state"] == "expired"
    assert run([doc(1, "kitas", d(-5))])["attention"][0]["state"] == "expired"


def test_kitap_label_and_code() -> None:
    item = run([doc(1, "visa", d(30), ocr_extracted_data=ocr("KITAP E28A"))])["attention"][0]
    assert (item["label"], item["code"], item["state"]) == ("KITAP", "E28A", "urgent")


def test_older_expired_kitas_hidden_by_newer() -> None:
    result = run([doc(1, "kitas", d(-200)), doc(2, "kitas", d(300))])
    assert result["attention"] == []


def test_lone_expired_kitas_is_shown() -> None:
    result = run([doc(1, "kitas", d(-200))])
    assert [(i["state"], i["document_id"]) for i in result["attention"]] == [("expired", 1)]


def test_supersession_ties_prefer_later_issue_then_upload() -> None:
    a = doc(1, "kitas", d(10), issue_date=d(-300))
    b = doc(2, "kitas", d(10), issue_date=d(-100))
    assert run([a, b])["attention"][0]["document_id"] == 2
    c = doc(3, "kitas", d(10), created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    e = doc(4, "kitas", d(10), created_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert run([c, e])["attention"][0]["document_id"] == 4


def test_newer_visit_visa_supersedes_expired_kitas() -> None:
    result = run([doc(1, "kitas", d(-10)), doc(2, "visa", d(60), ocr_extracted_data=ocr("VOA"))])
    assert [(i["label"], i["state"]) for i in result["attention"]] == [("Visit visa", "upcoming")]


def test_ignored_documents() -> None:
    for extra in (
        {"deleted_at": datetime(2026, 10, 1, tzinfo=timezone.utc)},
        {"is_archived": True},
        {"status": "rejected"},
    ):
        assert run([doc(1, "kitas", d(-5), **extra)])["attention"] == []
    # A deleted newer KITAS must not hide the live older one.
    deleted_newer = doc(2, "kitas", d(400), deleted_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert run([doc(1, "kitas", d(-5)), deleted_newer])["attention"][0]["document_id"] == 1


# ── visit visa and ITK ──────────────────────────────────────────────────────


def visit(doc_id: int = 1, expiry: date = d(20), **kw: Any) -> dict[str, Any]:
    return doc(doc_id, "visa", expiry, ocr_extracted_data=ocr("VOA"), **kw)


def test_visit_without_itk_is_flagged_itk_missing() -> None:
    item = run([visit(expiry=d(20))])["attention"][0]
    assert (item["state"], item["note"], item["date"]) == (
        "upcoming",
        "itk_missing",
        d(20).isoformat(),
    )
    assert run([visit(expiry=d(14))])["attention"][0]["state"] == "urgent"
    assert run([visit(expiry=d(15))])["attention"][0]["state"] == "upcoming"
    assert run([visit(expiry=d(0))])["attention"][0]["state"] == "expired"


def test_visit_with_itk_uses_itk_expiry() -> None:
    v = visit(expiry=d(5), issue_date=d(-30))
    itk = doc(2, "itk", d(100), issue_date=d(-10))
    item = run([v, itk])["attention"][0]
    assert (item["state"], item["note"], item["date"], item["document_id"]) == (
        "upcoming",
        None,
        d(100).isoformat(),
        2,
    )


def test_itk_issued_before_visa_does_not_extend_it() -> None:
    v = visit(expiry=d(5), issue_date=d(-30))
    old_itk = doc(2, "itk", d(100), issue_date=d(-60))
    item = run([v, old_itk])["attention"][0]
    assert item["note"] == "itk_missing" and item["state"] == "urgent"


def test_itk_without_issue_dates_uses_upload_order() -> None:
    v = visit(created_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
    later = doc(2, "itk", d(90), created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    earlier = doc(3, "itk", d(200), created_at=datetime(2026, 4, 1, tzinfo=timezone.utc))
    item = run([v, later, earlier])["attention"][0]
    assert item["document_id"] == 2 and item["note"] is None


# ── passport ────────────────────────────────────────────────────────────────


def test_passport_240_day_boundary_and_expired() -> None:
    cl = lambda n: {"id": 1, "full_name": "Client A", "passport_expiry": d(n)}  # noqa: E731
    assert run(client=cl(240))["attention"][0]["state"] == "urgent"
    assert run(client=cl(241))["attention"] == []
    assert run(client=cl(0))["attention"][0]["state"] == "expired"


def test_only_latest_passport_counts_and_dedupes_field_and_doc() -> None:
    client = {"id": 1, "full_name": "Client A", "passport_expiry": d(-100)}
    renewed = doc(7, "passport", d(2000))
    assert run([renewed], client=client)["attention"] == []
    same = doc(8, "passport", d(-100))
    items = run([same], client=client)["attention"]
    assert len(items) == 1 and items[0]["document_id"] == 8


# ── family members ──────────────────────────────────────────────────────────


def test_family_member_label_comes_from_real_permit() -> None:
    fam = [
        {
            "id": 5,
            "full_name": "Member B",
            "current_visa_type": "KITAS E31A",
            "visa_expiry": d(40),
            "passport_expiry": None,
        }
    ]
    item = run(family=fam)["attention"][0]
    assert (item["label"], item["subject"], item["subject_id"], item["subject_name"]) == (
        "KITAS",
        "family_member",
        5,
        "Member B",
    )
    assert item["code"] == "E31A"


def test_family_member_documents_do_not_leak_to_client() -> None:
    fam = [{"id": 5, "full_name": "Member B", "current_visa_type": None, "visa_expiry": None}]
    result = run([doc(1, "kitas", d(-3), family_member_id=5)], family=fam)
    assert [(i["subject"], i["subject_id"]) for i in result["attention"]] == [("family_member", 5)]


def test_unclassifiable_family_visa_gives_no_item() -> None:
    fam = [{"id": 5, "full_name": "Member B", "current_visa_type": "???", "visa_expiry": d(-3)}]
    assert run(family=fam)["attention"] == []


# ── LKPM ────────────────────────────────────────────────────────────────────

CO = [{"company_id": 9, "company_name": "PT Synthetic"}]


def rep(quarter: str, year: int, **kw: Any) -> dict[str, Any]:
    row = {
        "company_id": 9,
        "quarter": quarter,
        "year": year,
        "status": "draft",
        "oss_submitted": False,
        "has_receipts": False,
    }
    row.update(kw)
    return row


def lkpm_state(today: date, **kw: Any) -> dict[str, Any] | None:
    items = of_kind(
        run(today=today, companies=CO, lkpm=[rep("Q3", 2026, status="approved")], **kw), "lkpm"
    )
    return items[0] if items else None


def test_lkpm_boundaries_q4() -> None:
    assert lkpm_state(date(2026, 12, 21)) is None  # Q3 done+closed, Q4 countdown not begun
    up = lkpm_state(date(2026, 12, 22))
    assert (up["label"], up["state"], up["days"], up["opens_on"], up["date"]) == (
        "LKPM Q4 2026",
        "upcoming",
        10,
        "2027-01-01",
        "2027-01-15",
    )
    first = lkpm_state(date(2027, 1, 1))
    assert (first["state"], first["days"]) == ("open", 14)
    last = lkpm_state(date(2027, 1, 15))
    assert (last["state"], last["days"], last["date"]) == ("open", 0, "2027-01-15")
    over = lkpm_state(date(2027, 1, 16))
    assert (over["state"], over["days"]) == ("overdue", -1)
    # Overdue is time-boxed: close + 30 days still shown, +31 nothing.
    assert lkpm_state(date(2027, 2, 14))["state"] == "overdue"
    assert lkpm_state(date(2027, 2, 15)) is None
    assert lkpm_state(date(2027, 3, 21)) is None
    assert lkpm_state(date(2027, 3, 22))["label"] == "LKPM Q1 2027"


def test_lkpm_done_until_close_then_silent() -> None:
    reports = [rep("Q3", 2026, status="approved"), rep("Q4", 2026, oss_submitted=True)]
    kw = {"companies": CO, "lkpm": reports}
    assert of_kind(run(today=date(2027, 1, 10), **kw), "lkpm")[0]["state"] == "done"
    assert of_kind(run(today=date(2027, 1, 15), **kw), "lkpm")[0]["state"] == "done"
    assert of_kind(run(today=date(2027, 1, 16), **kw), "lkpm") == []


def test_lkpm_done_by_status_or_receipt() -> None:
    for flag in ({"status": "submitted"}, {"has_receipts": True}):
        got = of_kind(
            run(
                today=date(2027, 1, 5),
                companies=CO,
                lkpm=[rep("Q3", 2026, status="approved"), rep("Q4", 2026, **flag)],
            ),
            "lkpm",
        )
        assert got[0]["state"] == "done"
    pending = of_kind(
        run(
            today=date(2027, 1, 5),
            companies=CO,
            lkpm=[rep("Q3", 2026, status="approved"), rep("Q4", 2026, status="pending")],
        ),
        "lkpm",
    )
    assert pending[0]["state"] == "open"


def test_no_lkpm_rows_no_lkpm_item() -> None:
    assert of_kind(run(today=date(2027, 1, 5), companies=CO), "lkpm") == []


# ── SPT ─────────────────────────────────────────────────────────────────────

TAX = {"id": 1, "full_name": "Client A", "tax_consultant": "staff@example.test"}


def spt(kind: str, today: date, docs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    r = run(docs, today=today, client=TAX, companies=CO)
    items = of_kind(r, kind)
    return items[0] if items else None


def test_spt_personal_boundaries() -> None:
    assert spt("spt_personal", date(2026, 2, 28)) is None
    first = spt("spt_personal", date(2026, 3, 1))
    assert (first["state"], first["days"], first["label"]) == ("upcoming", 30, "SPT personal 2025")
    assert spt("spt_personal", date(2026, 3, 20))["state"] == "upcoming"
    assert spt("spt_personal", date(2026, 3, 21))["state"] == "urgent"
    assert spt("spt_personal", date(2026, 3, 31))["state"] == "urgent"
    assert spt("spt_personal", date(2026, 4, 1))["state"] == "overdue"
    assert spt("spt_personal", date(2026, 4, 30))["state"] == "overdue"  # deadline + 30
    assert spt("spt_personal", date(2026, 5, 1)) is None  # deadline + 31
    assert spt("spt_personal", date(2026, 12, 31)) is None
    assert spt("spt_personal", date(2027, 1, 5)) is None


def test_spt_personal_done_needs_matching_fiscal_year() -> None:
    ok = doc(1, "spt_personal", None, fiscal_year=2025)
    assert spt("spt_personal", date(2026, 4, 10), [ok])["state"] == "done"
    wrong_year = doc(1, "spt_personal", None, fiscal_year=2024)
    assert spt("spt_personal", date(2026, 4, 10), [wrong_year])["state"] == "overdue"
    deleted = doc(
        1,
        "spt_personal",
        None,
        fiscal_year=2025,
        deleted_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert spt("spt_personal", date(2026, 4, 10), [deleted])["state"] == "overdue"


def test_spt_company_boundaries_and_done() -> None:
    assert spt("spt_company", date(2026, 3, 30)) is None
    first = spt("spt_company", date(2026, 3, 31))
    assert (first["state"], first["days"]) == ("upcoming", 30)
    assert spt("spt_company", date(2026, 4, 1))["state"] == "upcoming"
    assert spt("spt_company", date(2026, 4, 19))["state"] == "upcoming"
    assert spt("spt_company", date(2026, 4, 20))["state"] == "urgent"
    assert spt("spt_company", date(2026, 5, 1))["state"] == "overdue"
    assert spt("spt_company", date(2026, 5, 30))["state"] == "overdue"  # deadline + 30
    assert spt("spt_company", date(2026, 5, 31)) is None  # deadline + 31
    item = spt("spt_company", date(2026, 5, 1), [doc(1, "spt", None, fiscal_year=2025)])
    assert (item["state"], item["label"], item["subject_id"]) == ("done", "SPT company 2025", 9)


def test_non_tax_client_gets_no_spt() -> None:
    r = run(today=date(2026, 4, 10), client={"id": 1, "full_name": "Client A"}, companies=CO)
    assert of_kind(r, "spt_personal") == [] and of_kind(r, "spt_company") == []


def test_lkpm_rows_make_a_tax_client() -> None:
    r = run(today=date(2026, 4, 10), companies=CO, lkpm=[rep("Q1", 2026)])
    assert of_kind(r, "spt_personal")[0]["state"] == "overdue"
    assert of_kind(r, "spt_company")[0]["state"] == "upcoming"


# ── ordering and stats ──────────────────────────────────────────────────────


def test_sort_and_stats() -> None:
    docs = [
        doc(1, "kitas", d(-5)),  # expired
        doc(2, "visa", d(100), ocr_extracted_data=ocr("VOA"), family_member_id=5),  # upcoming
        doc(3, "passport", d(50), family_member_id=5),  # urgent
        doc(4, "spt_personal", None, fiscal_year=2025),  # done
    ]
    fam = [{"id": 5, "full_name": "Member B", "current_visa_type": None, "visa_expiry": None}]
    r = run(docs, client=TAX, family=fam)
    states = [i["state"] for i in r["attention"]]
    assert states == sorted(
        states, key=["expired", "overdue", "urgent", "open", "upcoming", "done"].index
    )
    assert r["attention_stats"] == {"expired": 1, "urgent": 1, "upcoming": 1, "done": 1}
    # every item carries the full contract shape
    keys = {
        "kind",
        "subject",
        "subject_id",
        "subject_name",
        "label",
        "code",
        "state",
        "date",
        "opens_on",
        "days",
        "note",
        "document_id",
    }
    assert all(set(i) == keys for i in r["attention"])


def test_lkpm_overdue_only_for_companies_that_track_lkpm() -> None:
    draft_only = [rep("Q3", 2026)]
    tracked = [rep("Q3", 2026), rep("Q2", 2026, oss_submitted=True)]
    receipt = [rep("Q3", 2026), rep("Q2", 2026, has_receipts=True)]
    after_close = date(2026, 10, 20)
    assert of_kind(run(today=after_close, companies=CO, lkpm=draft_only), "lkpm") == []
    for reports in (tracked, receipt):
        got = of_kind(run(today=after_close, companies=CO, lkpm=reports), "lkpm")
        assert [(i["label"], i["state"]) for i in got] == [("LKPM Q3 2026", "overdue")]
    # Untracked companies still get the countdown and the open window.
    assert (
        of_kind(run(today=date(2026, 9, 25), companies=CO, lkpm=draft_only), "lkpm")[0]["state"]
        == "upcoming"
    )
    assert (
        of_kind(run(today=date(2026, 10, 5), companies=CO, lkpm=draft_only), "lkpm")[0]["state"]
        == "open"
    )


def test_lkpm_tracking_expires_after_twelve_months() -> None:
    stale = [rep("Q3", 2025, status="approved"), rep("Q3", 2026)]
    # Q3 2025 window closed 2025-10-15; on 2026-10-20 that is 370 days ago.
    assert of_kind(run(today=date(2026, 10, 20), companies=CO, lkpm=stale), "lkpm") == []
    recent = [rep("Q4", 2025, status="approved"), rep("Q3", 2026)]
    got = of_kind(run(today=date(2026, 10, 20), companies=CO, lkpm=recent), "lkpm")
    assert got[0]["state"] == "overdue"

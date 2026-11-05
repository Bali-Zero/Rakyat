"""Template + validator tests (guilt and innocence), synthetic payloads only."""

from datetime import date

import pytest

from backend.services.crm import client_recap_text as t

IMM = {
    "current_permit": {"code": "E23", "official_name": "E23 - Working KITAS",
                       "issue_date": "2026-03-14", "expiry_date": "2027-03-14"},
    "itk_expiry_date": "2027-01-15",
    "previous_permits_count": 1,
    "previous_permit_codes": ["E23"],
    "passport_expiry_date": "2027-06-01",
    "family_members_count": 2,
    "family_permits": [{"relationship": "spouse", "permit_code": "E31", "permit_expiry_date": "2027-01-01"},
                       {"relationship": "child", "permit_code": "E31", "permit_expiry_date": "2028-01-01"}],
    "open_practices": [{"type": "KITAS_EXTENSION", "status": "on_process"}],
}  # fmt: skip

CO = {
    "legal_form": "PT PMA", "established_date": "2024-02-01",
    "authorized_capital_idr": 10_000_000_000, "paid_up_capital_idr": 2_500_000_000,
    "shareholders_count": 2, "directors_count": 1,
    "kbli": [{"code": "62010", "title": "Computer Programming", "in_2025": True},
             {"code": "74100", "title": "Design Activities", "in_2025": False}],
    "lkpm_latest": {"year": 2026, "quarter": "Q2", "status": "approved"},
    "last_amendment_date": "2025-06-01",
}  # fmt: skip


def test_immigration_template_content_and_shape():
    out = t.template_recap("immigration", IMM)
    assert "Working KITAS (E23)" in out and "14 March 2026" in out and "14 March 2027" in out
    assert "an immigration practice is open" in out.lower()
    assert "15 January 2027" in out and "1 June 2027" in out
    assert "one of them with a permit that ends before the main permit" in out
    assert len(out) <= t.MAX_CHARS
    assert t.validate_recap(out, IMM)[0], t.validate_recap(out, IMM)


def test_passport_far_after_permit_is_omitted_and_kitas_gates_itk():
    p = dict(IMM, passport_expiry_date="2031-01-01")
    assert "passport" not in t.template_recap("immigration", p)
    p = {"current_permit": {"code": "D12", "official_name": "D12 - Pre-Investment", "expiry_date": "2027-03-14"},
         "itk_expiry_date": "2027-01-15"}  # fmt: skip
    assert "ITK" not in t.template_recap("immigration", p)


def test_company_template_content_and_shape():
    out = t.template_recap("company", CO)
    assert "PT PMA" in out and "1 February 2024" in out
    assert "IDR 10.0 billion" in out and "IDR 2.5 billion" in out
    assert "two shareholders and one director" in out
    assert "two KBLI lines" in out and "one code still on KBLI 2020" in out
    assert "Q2 2026" in out and "1 June 2025" in out
    assert t.validate_recap(out, CO)[0], t.validate_recap(out, CO)


def test_partial_and_empty_payloads():
    assert t.template_recap("company", {}) == ""
    out = t.template_recap("immigration", {"passport_expiry_date": "2029-05-05"})
    assert "5 May 2029" in out and "recorded" in out
    assert t.template_recap("company", {"legal_form": "PT"}) == "The company is a PT."
    assert t.based_on("immigration", IMM) == [
        "current permit",
        "ITK",
        "passport",
        "family",
        "practices",
        "previous permits",
    ]
    assert t.based_on("company", {}) == []


def test_idr_format():
    assert t.fmt_idr(10_000_000_000) == "IDR 10.0 billion"
    assert t.fmt_idr(500_000_000) == "IDR 500.0 million"
    assert t.fmt_idr(12_345) == "IDR 12,345"


GOOD = "The permit is E23 and runs until 14 March 2027. Two family members are on file."


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("The permit E23 ends on 15 March 2027. It is valid.", "invented_date"),
        ("The permit E23 ends on 2027-03-15. It is valid.", "invented_date"),
        ("The permit ends on 03/15/2027. It is valid.", "invented_date"),
        ("The permit E99 ends on 14 March 2027. It is valid.", "invented_code"),
        ("The permit E23 ends on 14 March 2027. It has 7 relatives.", "invented_number"),
        ("The company holds IDR 99.0 billion. It is a PT.", "invented_amount"),
        ("The permit E23 ends in 3 months. It is valid.", "relative_time"),
        ("The permit E23 ends soon. It is valid.", "relative_time"),
        ("The permit E23 is fine.", "sentence_count_1"),
        ("One. Two. Three. Four.", "sentence_count_4"),
        ("The permit E23 is valid. " + "x" * 450 + ".", "too_long"),
        ("**The permit E23 is valid.** It is fine.", "markdown"),
        ("", "empty"),
    ],
)
def test_validator_rejects(text, reason):
    ok, reasons = t.validate_recap(text, IMM)
    assert not ok and reason in reasons, reasons


def test_validator_accepts_good_variants():
    assert t.validate_recap(GOOD, IMM) == (True, [])
    assert t.validate_recap(
        "Permit E23 expires on March 14, 2027. The ITK runs to 15 Jan 2027.", IMM
    )[0]
    assert t.validate_recap("Capital is IDR 10 billion. The company has 2 shareholders.", CO)[0]
    assert t.validate_recap("It uses KBLI 62010. One code is still on KBLI 2020.", CO)[0]
    assert t.validate_recap(
        "Permit E23 is valid from March 2026. The permit ends 14 March 2027.", IMM
    )[0]


def test_permit_name_and_family_code_cleaning():
    from backend.services.crm import client_recap as r

    assert r.clean_permit_name("Investor KITAS (Standard)") == "Investor KITAS"
    assert r.clean_permit_name("KITAS / ITAS") == "KITAS"
    assert r.clean_permit_name("Working  KITAS (E23) (x)") == "Working KITAS"
    p = r.recap_payload(
        "immigration",
        {"current_permit": {"code": "KITAS / ITAS", "official_name": "Investor KITAS (Standard)"}},
    )
    assert p["current_permit"] == {"code": "KITAS", "official_name": "Investor KITAS"}
    assert "(Standard)" not in t.template_recap("immigration", p)


def test_expired_status_in_template_and_allow_list():
    cp = {"code": "E23", "expiry_date": "2026-01-10", "status": "expired"}
    p = {"current_permit": cp}
    from backend.services.crm import client_recap as r

    assert r.recap_payload("immigration", p)["current_permit"]["status"] == "expired"
    assert "expired on 10 January 2026" in t.template_recap("immigration", p)
    p["current_permit"] = dict(cp, status="valid")
    assert "valid until 10 January 2026" in t.template_recap("immigration", p)
    assert (
        "status"
        not in r.recap_payload("immigration", {"current_permit": dict(cp, status="weird")})[
            "current_permit"
        ]
    )


def _doc(kind, expiry, issue=None):
    ocr = {
        "raw_response": {
            "visa_type": "E23 Working KITAS",
            **({"issue_date": issue} if issue else {}),
        }
    }
    return {"document_type": kind, "status": "active", "expiry_date": date.fromisoformat(expiry),
            "created_at": None, "family_member_id": None, "ocr_extracted_data": ocr if kind == "visa" else None}  # fmt: skip


def test_builder_status_and_itk_belongs_to_current_permit():
    from backend.services.crm import client_recap as r

    def build(docs, today=date(2026, 11, 4)):
        return r.build_immigration_facts(
            documents=docs,
            family_members=[],
            practices=[],
            passport_expiry=None,
            catalogue=None,
            today=today,
        )

    f = build([_doc("visa", "2027-06-01", "2026-06-02"), _doc("itk", "2023-01-01")])
    assert f["current_permit"]["status"] == "valid" and "itk_expiry_date" not in f
    f = build([_doc("visa", "2027-06-01", "2026-06-02"), _doc("itk", "2027-01-01")])
    assert f["itk_expiry_date"] == "2027-01-01"
    f = build([_doc("visa", "2027-06-01"), _doc("itk", "2027-01-01")])
    assert (
        f["itk_expiry_date"] == "2027-01-01"
    )  # unknown issue date: not yet expired, inside validity
    assert "itk_expiry_date" not in build([_doc("visa", "2027-06-01"), _doc("itk", "2023-01-01")])
    f = build([_doc("visa", "2027-06-01"), _doc("itk", "2028-01-01")])
    assert "itk_expiry_date" not in f
    assert build([_doc("visa", "2026-01-01")])["current_permit"]["status"] == "expired"
    assert "itk_expiry_date" not in build([_doc("itk", "2027-01-01")])


def test_practice_prints_type_only():
    from backend.services.crm import client_recap as r

    p = r.recap_payload(
        "immigration",
        {"open_practices": [{"type": "KITAS Extension", "status": "sending invoices"}]},
    )
    assert p["open_practices"] == [{"type": "KITAS Extension"}]
    out = t.template_recap("immigration", p)
    assert ": KITAS Extension" in out and "invoice" not in out


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Your permit E23 is valid. It ends 14 March 2027.", "second_person"),
        ("The permit E23 is valid. As stated, it ends 14 March 2027.", "meta_phrase"),
        ("The permit E23 is valid. The applicant waits.", "meta_phrase"),
        ("The permit E23 is valid. The facts show 14 March 2027.", "meta_phrase"),
    ],
)
def test_validator_second_person_and_meta(text, reason):
    ok, reasons = t.validate_recap(text, IMM)
    assert not ok and reason in reasons, reasons


@pytest.mark.asyncio
async def test_retry_once_with_reasons():
    class P:
        name, model = "p", "m"

        def __init__(self, outs):
            self.outs, self.prompts = list(outs), []

        async def generate(self, prompt):
            self.prompts.append(prompt)
            return self.outs.pop(0)

    bad, good = "Your permit E23 is valid. It ends soon.", GOOD
    p = P([bad, good])
    text, ok, first, _ = await t.rewrite_with_retry(p, "tpl", IMM)
    assert (
        ok
        and not first
        and text == good
        and "rejected for" in p.prompts[1]
        and "second_person" in p.prompts[1]
    )
    p = P([bad, bad])
    _, ok, first, reasons = await t.rewrite_with_retry(p, "tpl", IMM)
    assert not ok and not first and "second_person" in reasons and len(p.prompts) == 2
    p = P([good])
    assert (await t.rewrite_with_retry(p, "tpl", IMM))[2] is True and len(p.prompts) == 1


TODAY = date(2026, 11, 4)


@pytest.mark.parametrize(
    ("scope", "text", "reason"),
    [
        (
            "immigration",
            "The company holds a permit E23. It ends on 14 March 2027.",
            "wrong_subject",
        ),
        (
            "company",
            "The client is a PT PMA. It was established on 1 February 2024.",
            "wrong_subject",
        ),
        (
            "immigration",
            "The permit E23 is shown in the draft. It ends on 14 March 2027.",
            "leak_phrase",
        ),
        ("immigration", "The permit E23 is valid. All dates are absolute values.", "leak_phrase"),
        (
            "immigration",
            "The permit E23 stays valid. Its expiry is specified elsewhere.",
            "leak_phrase",
        ),
        (
            "immigration",
            "The permit E23 is valid. The passport remains expired on 1 June 2027.",
            "expired_in_future",
        ),
        (
            "immigration",
            "The permit E23 expired on 14 March 2027. The client waits.",
            "expired_in_future",
        ),
        (
            "immigration",
            "The permit E23 is valid until 14 March 2025. Nothing else.",
            "valid_in_past",
        ),
        (
            "immigration",
            "The permit E23 is valid until the passport expiry date of 1 June 2027. Nothing else.",
            "passport_date_misplaced",
        ),
        (
            "immigration",
            "The passport expires on 14 March 2027. The permit E23 is valid.",
            "permit_date_misplaced",
        ),
        (
            "immigration",
            "The permit E23 is valid. The passport expires on 1 June 2027 and the permit is E23.",
            "passport_date_misplaced",
        ),
    ],
)
def test_validator_semantic_rejects(scope, text, reason):
    payload = IMM if scope == "immigration" else CO
    ok, reasons = t.validate_recap(text, payload, scope, TODAY)
    assert not ok and reason in reasons, reasons


def test_validator_semantic_accepts_and_draft_lkpm_exception():
    good = "The client's current permit is Working KITAS (E23), issued on 14 March 2026 and valid until 14 March 2027. The passport expires on 1 June 2027."
    assert t.validate_recap(good, IMM, "immigration", TODAY) == (True, [])
    co = dict(CO, lkpm_latest={"year": 2026, "quarter": "Q2", "status": "draft"})
    ok, _ = t.validate_recap(
        "The company is a PT PMA. The latest LKPM is Q2 2026 (draft).", co, "company", TODAY
    )
    assert ok
    ok, reasons = t.validate_recap(
        "The company is a PT PMA. The latest LKPM is Q2 2026 (draft).", CO, "company", TODAY
    )
    assert not ok and "leak_phrase" in reasons


def test_templates_read_naturally_and_pass_the_validator():
    imm = t.template_recap("immigration", IMM, TODAY)
    assert (
        imm.startswith("The client's current permit is")
        and ";" not in imm
        and "company" not in imm.lower()
    )
    assert "two family members are on file" in imm and "one earlier permit is on record" in imm
    assert t.validate_recap(imm, IMM, "immigration", TODAY) == (True, [])
    co = t.template_recap("company", CO, TODAY)
    assert ";" not in co and "client" not in co.lower()
    assert t.validate_recap(co, CO, "company", TODAY) == (True, [])
    past = dict(IMM, itk_expiry_date="2026-01-01", passport_expiry_date="2026-02-01")
    out = t.template_recap("immigration", past, TODAY)
    assert (
        "ITK stamp expired on 1 January 2026" in out
        and "passport expired on 1 February 2026" in out
    )
    assert t.validate_recap(out, past, "immigration", TODAY)[0]


def test_expired_permit_says_latest_and_practices_list_without_nested_parens():
    cp = {
        "code": "E23",
        "official_name": "E23 - Working KITAS",
        "expiry_date": "2026-01-10",
        "status": "expired",
    }
    out = t.template_recap("immigration", {"current_permit": cp}, TODAY)
    assert "latest permit is Working KITAS (E23)" in out and "current permit" not in out
    p = {"open_practices": [{"type": "A"}, {"type": "B"}, {"type": "A"}]}
    assert "Three immigration practices are open, including A and B" in t.template_recap(
        "immigration", p, TODAY
    )
    p = {"open_practices": [{"type": "A"}, {"type": "B"}]}
    assert "Two immigration practices are open: A and B" in t.template_recap(
        "immigration", p, TODAY
    )


@pytest.mark.parametrize(
    "text",
    [
        "The company is a PT PMA. The last amendment runs until 1 June 2025.",
        "The company is a PT PMA. It was established and valid until 1 June 2025.",
        "The company is a PT PMA. No amendments are in place.",
        "The company is a PT PMA. Its structure remains unchanged.",
    ],
)
def test_validator_rejects_validity_on_non_validity_items(text):
    ok, reasons = t.validate_recap(text, CO, "company", TODAY)
    assert not ok, reasons


def test_number_words_are_checked_against_the_payload():
    ok, reasons = t.validate_recap(
        "The permit E23 is valid. Four family members are on file.", IMM, "immigration", TODAY
    )
    assert not ok and "invented_number" in reasons
    ok, reasons = t.validate_recap(
        "The permit E23 is valid. Two family members are on file.", IMM, "immigration", TODAY
    )
    assert "invented_number" not in reasons


def test_rewrite_must_keep_the_template_facts():
    tpl = t.template_recap("immigration", IMM, TODAY)
    assert t.validate_recap(tpl, IMM, "immigration", TODAY, template=tpl)[0]
    dropped_date = tpl.replace("14 March 2026", "").replace(" issued on  and", "")
    ok, reasons = t.validate_recap(
        "The client's current permit is Working KITAS (E23), valid until 14 March 2027. Two family members are on file.",
        IMM,
        "immigration",
        TODAY,
        template=tpl,
    )
    assert not ok and any(x.startswith("dropped_") for x in reasons), reasons
    assert dropped_date
    exp = {
        "current_permit": {
            "code": "E23",
            "official_name": "Working KITAS",
            "expiry_date": "2026-01-10",
            "status": "expired",
        }
    }
    tpl2 = t.template_recap("immigration", exp, TODAY)
    ok, reasons = t.validate_recap(
        "The client's latest permit is Working KITAS (E23). Nothing else is recorded.",
        exp,
        "immigration",
        TODAY,
        template=tpl2,
    )
    assert not ok and "dropped_date" in reasons
    ok, reasons = t.validate_recap(
        "The client's latest permit is Working KITAS (E23), valid on 10 January 2026. Nothing else is recorded.",
        exp,
        "immigration",
        TODAY,
        template=tpl2,
    )
    assert not ok and "dropped_status" in reasons
    ok, reasons = t.validate_recap(
        "The client's latest permit, Working KITAS (E23), expired on 10 January 2026. Nothing else is recorded.",
        exp,
        "immigration",
        TODAY,
        template=tpl2,
    )
    assert ok, reasons


def test_rewrite_dropping_codes_names_and_counts_fails():
    tpl = t.template_recap("company", CO, TODAY)
    keep = tpl
    assert t.validate_recap(keep, CO, "company", TODAY, template=tpl)[0]
    no_amount = tpl.replace("IDR 10.0 billion", "a large amount")
    ok, reasons = t.validate_recap(no_amount, CO, "company", TODAY, template=tpl)
    assert not ok and "dropped_amount" in reasons
    no_count = tpl.replace("two shareholders and one director", "several owners")
    ok, reasons = t.validate_recap(no_count, CO, "company", TODAY, template=tpl)
    assert not ok and "dropped_count" in reasons


def test_status_fields_drive_template_wording():
    p = {"passport_expiry_date": "2026-11-10", "passport_status": "expired"}
    assert "passport expired on 10 November 2026" in t.template_recap(
        "immigration", p, date(2026, 11, 4)
    )
    p = {"passport_expiry_date": "2026-11-10", "passport_status": "valid"}
    assert "passport expires on 10 November 2026" in t.template_recap(
        "immigration", p, date(2027, 1, 1)
    )

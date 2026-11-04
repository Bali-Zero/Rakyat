"""Bridging: three qualified transitions, onshore/deadline gates and unknown sponsor."""

import json

import pytest

from backend.services.visa_engine.families import guide
from backend.services.visa_engine.families.guide import NOT_COVERED, TO_VERIFY, resolve

DATA = guide.load_family("BRIDGING")
CARD = json.loads((guide.HERE / "law" / "BRIDGING" / "BRIDGING.json").read_text())
# Expected routes written from the source quotation, independently of the configuration.
ROUTES = (
    "evisa_visit_to_itas",
    "nonextendable_itas_to_new_permit",
    "nonextendable_itap_to_new_permit",
)


def answers(route: str, **extra: object) -> dict:
    return {"permit_transition": route, "onshore": "yes", "application_deadline": "yes", **extra}


@pytest.mark.parametrize("route", ROUTES)
def test_each_leaf_asks_three_questions_then_requires_verification(route: str) -> None:
    assert resolve("BRIDGING", {}) == {"ask": "permit_transition"}
    a = {"permit_transition": route}
    assert resolve("BRIDGING", a) == {"ask": "onshore", "code": "BRIDGING"}
    a["onshore"] = "yes"
    assert resolve("BRIDGING", a) == {"ask": "application_deadline", "code": "BRIDGING"}
    a["application_deadline"] = "yes"
    assert resolve("BRIDGING", a) == {"code": "BRIDGING", "verdict": TO_VERIFY}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("question", ["onshore", "application_deadline"])
def test_offshore_departure_or_missed_application_payment_deadline_rejected(
    route: str, question: str
) -> None:
    assert resolve("BRIDGING", answers(route, **{question: "no"})) == {
        "code": "BRIDGING",
        "verdict": NOT_COVERED,
    }


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("question", ["permit_transition", "onshore", "application_deadline"])
@pytest.mark.parametrize("value", [None, "", "unknown", True, False, 0, 1])
def test_missing_or_invalid_answers_never_qualify(route: str, question: str, value: object) -> None:
    result = resolve("BRIDGING", answers(route, **{question: value}))
    assert result == {
        "ask": question,
        **({"code": "BRIDGING"} if question != "permit_transition" else {}),
    }


@pytest.mark.parametrize(
    "route",
    ["visa_free_to_itas", "voa_to_itas", "any_visit_to_itas", "extendable_itas", "itas_to_itap"],
)
def test_unstated_transitions_are_not_inferred(route: str) -> None:
    assert resolve("BRIDGING", answers(route)) == {"ask": "permit_transition"}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("sponsor", ["yes", "no", "individual", "company"])
def test_extra_answers_cannot_resolve_unstated_nationwide_sponsor_rule(
    route: str, sponsor: str
) -> None:
    assert resolve(
        "BRIDGING", answers(route, sponsor=sponsor, travel_document="national_passport")
    ) == {
        "code": "BRIDGING",
        "verdict": TO_VERIFY,
    }


def test_question_scope_and_stays_match_verbatim_law() -> None:
    assert set(DATA["questions"]) == {"permit_transition", "onshore", "application_deadline"}
    assert DATA["questions"]["permit_transition"]["options"] == dict.fromkeys(ROUTES, "BRIDGING")
    assert set(DATA["visas"]) == {"BRIDGING"}
    quote = CARD["ketentuan_lain_verbatim"][0]
    assert "Izin Tinggal Kunjungan yang diajukan melalui evisa.imigrasi.go.id" in quote
    assert "memperoleh Izin Tinggal Terbatas" in quote
    assert (
        "Izin Tinggal Terbatas dan Izin Tinggal Tetap yang sudah tidak bisa lagi diperpanjang"
        in quote
    )
    assert "dapat memperoleh Izin Tinggal baru" in quote
    assert "60 hari" in CARD["stay_days"][0]
    assert "hanya berlaku secara onshore" in CARD["where_to_apply"]
    assert (
        "tidak berlaku lagi apabila WNA keluar wilayah Indonesia"
        in CARD["prohibited_or_conditions_verbatim"][0]
    )
    assert "melakukan pembayaran" in CARD["pengajuan_intro_verbatim"]
    assert "paling lambat 3 (tiga) hari sebelum" in CARD["pengajuan_intro_verbatim"]
    deadline = DATA["questions"]["application_deadline"]["text_en"]
    assert "apply through evisa.imigrasi.go.id and pay" in deadline
    assert "no later than 3 days before" in deadline
    visa = DATA["visas"]["BRIDGING"]
    assert visa["stay_days"] == 60
    assert visa["stay_options"] == [{"value": 60, "unit": "DAY"}]
    assert visa["sponsor_required"] is CARD["sponsor_required"]["as_stated_boolean"] is None
    # Owner ruling 2026-10-19: not extendable, per Permenkumham 11/2024 Pasal 86A ayat (1);
    # the page extraction's own extendable field stays null.
    assert CARD["extendable"] is None
    assert visa["extendable"] is False
    (regulation,) = CARD["regulation_verbatim"]
    assert regulation["article"] == "Pasal 86A ayat (1)"
    assert regulation["text"].endswith(
        "paling lama 60 (enam puluh) hari dan tidak dapat diperpanjang."
    )
    assert regulation["url"] == "https://peraturan.go.id/files/permenkumham-no-11-tahun-2024.pdf"
    assert CARD["owner_rulings"][0]["date"] == "2026-10-19"
    assert not any(t.startswith("extendable:") for t in CARD["to_verify"])
    assert any(
        n.startswith("OWNER RULING 2026-10-19: BRIDGING is not extendable") for n in DATA["notes"]
    )
    assert visa["max_total_days"] is visa["max_total_months"] is CARD["total_max_verbatim"] is None
    assert visa["entry_type"] is CARD["entry"] is None


def test_local_documents_remain_a_checklist_and_approval_after_expiry_is_not_a_waiver() -> None:
    assert len(CARD["requirements_verbatim"]) == 9
    local = CARD["requirements_verbatim"][1:]
    assert all("JAKARTA_PUSAT" in item["section"] and item["source_url"] for item in local)
    assert "KTP Sponsor" in [item["text"] for item in local]
    assert "Form E02" in local[-2]["text"]
    assert (
        "disetujui setelah masa berlaku izin tinggal sebelumnya berakhir"
        in CARD["allowed_activities"]
    )
    assert (
        resolve("BRIDGING", answers(ROUTES[0], application_deadline="no"))["verdict"] == NOT_COVERED
    )

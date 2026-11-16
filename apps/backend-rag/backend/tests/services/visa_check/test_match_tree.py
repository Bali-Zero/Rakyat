"""Unit tests for the Visa Match decision tree.

Covers every branch, the ranked-result shape, and backwards-compat
property accessors used by the router.
"""

from __future__ import annotations

from backend.services.visa_check import match_tree
from backend.services.visa_check.catalogue import VISA_META, VisaType
from backend.services.visa_check.match_tree import (
    BudgetBand,
    MatchResult,
    Purpose,
    RankedVisa,
    WorkActivity,
    recommend_visa,
)


def _call(
    *,
    purpose: Purpose,
    duration_months: int = 6,
    budget_band: BudgetBand = BudgetBand.MID_50_500M,
    nationality: str = "USA",
) -> MatchResult:
    return recommend_visa(
        nationality=nationality,
        purpose=purpose,
        duration_months=duration_months,
        budget_band=budget_band,
    )


class TestRankedShape:
    def test_ranking_is_list_of_rankedvisa(self):
        r = _call(purpose=Purpose.LONG_TOURISM, duration_months=1)
        assert isinstance(r.ranking, list)
        for item in r.ranking:
            assert isinstance(item, RankedVisa)

    def test_ranking_sorted_by_score_desc(self):
        r = _call(purpose=Purpose.WORK_REMOTE, budget_band=BudgetBand.MID_50_500M)
        scores = [rv.score for rv in r.ranking]
        assert scores == sorted(scores, reverse=True)

    def test_every_ranked_visa_is_in_visa_meta(self):
        for purpose in Purpose:
            if purpose == Purpose.OTHER:
                continue
            for band in BudgetBand:
                r = _call(purpose=purpose, duration_months=6, budget_band=band)
                for rv in r.ranking:
                    assert rv.visa in VISA_META, f"{rv.visa} not in VISA_META"

    def test_scores_in_unit_range(self):
        r = _call(purpose=Purpose.INVESTOR, budget_band=BudgetBand.OVER_500M)
        for rv in r.ranking:
            assert 0.0 <= rv.score <= 1.0


class TestBackwardsCompatProperties:
    def test_recommended_visa_is_ranking_zero(self):
        r = _call(purpose=Purpose.LONG_TOURISM, duration_months=1)
        if r.ranking:
            assert r.recommended_visa == r.ranking[0].visa
        else:
            assert r.recommended_visa is None

    def test_alternatives_is_ranking_tail(self):
        r = _call(purpose=Purpose.INVESTOR, budget_band=BudgetBand.OVER_500M)
        expected = [rv.visa for rv in r.ranking[1:]]
        assert r.alternatives == expected

    def test_reason_is_ranking_zero_reason(self):
        r = _call(purpose=Purpose.LONG_TOURISM, duration_months=1)
        if r.ranking:
            assert r.reason == r.ranking[0].reason


class TestOther:
    def test_other_always_refers(self):
        r = _call(purpose=Purpose.OTHER)
        assert r.recommended_visa is None
        assert r.referral_mode is True
        assert r.ranking == []
        assert "WhatsApp" in r.reason


class TestLongTourism:
    def test_short_trip_is_B1(self):
        r = _call(purpose=Purpose.LONG_TOURISM, duration_months=1)
        assert r.recommended_visa is VisaType.B1
        assert r.referral_mode is False

    def test_medium_trip_stays_within_tourism_set(self):
        r = _call(purpose=Purpose.LONG_TOURISM, duration_months=5)
        # Must pick a C-series or referral — never B211A (it's gone).
        allowed = {VisaType.C1, VisaType.C2, VisaType.C6, None}
        assert r.recommended_visa in allowed

    def test_too_long_tourism_refers(self):
        r = _call(purpose=Purpose.LONG_TOURISM, duration_months=10)
        assert r.recommended_visa is None
        assert r.referral_mode is True


class TestWorkRemote:
    def test_under_budget_still_produces_ranking(self):
        r = _call(purpose=Purpose.WORK_REMOTE, budget_band=BudgetBand.UNDER_50M)
        # Visit visas (C1/C2/D2) forbid remote work (P19); under-budget remote workers
        # get a specific WhatsApp referral naming the IDR 50,000,000 floor instead.
        assert r.referral_mode is True
        assert r.recommended_visa is None
        assert not {rv.visa for rv in r.ranking} & {VisaType.C1, VisaType.C2, VisaType.D2}
        assert "50,000,000" in r.reason
        assert "whatsapp" in r.reason.lower()

    def test_mid_budget_is_E33G(self):
        r = _call(purpose=Purpose.WORK_REMOTE, budget_band=BudgetBand.MID_50_500M)
        assert r.recommended_visa is VisaType.E33G

    def test_high_budget_is_E33G(self):
        r = _call(purpose=Purpose.WORK_REMOTE, budget_band=BudgetBand.OVER_500M)
        assert r.recommended_visa is VisaType.E33G

    def test_impresariat_never_offered_from_generic_work_remote(self):
        for band in BudgetBand:
            r = _call(purpose=Purpose.WORK_REMOTE, budget_band=band)
            assert VisaType.E23_FREELANCE not in [rv.visa for rv in r.ranking]

    def test_mid_budget_e33g_reason_states_annual_income_not_savings(self):
        r = _call(purpose=Purpose.WORK_REMOTE, budget_band=BudgetBand.MID_50_500M)
        e33g = next((rv for rv in r.ranking if rv.visa == VisaType.E33G), None)
        assert e33g is not None
        assert "USD 60,000 a year" in e33g.reason
        assert "salary or income" in e33g.reason
        assert "savings" not in e33g.reason.lower()
        assert r.reason == e33g.reason

    def test_mid_budget_e33g_steps_state_annual_income_not_balance(self):
        r = _call(purpose=Purpose.WORK_REMOTE, budget_band=BudgetBand.MID_50_500M)
        assert r.recommended_visa is VisaType.E33G
        assert any("USD 60,000 a year" in s for s in r.pre_arrival_steps)
        for step in r.pre_arrival_steps:
            lowered = step.lower()
            assert "balance" not in lowered
            assert "savings" not in lowered

    def test_over_budget_e33g_steps_state_annual_income_not_balance(self):
        r = _call(purpose=Purpose.WORK_REMOTE, budget_band=BudgetBand.OVER_500M)
        assert r.recommended_visa is VisaType.E33G
        assert any("USD 60,000 a year" in s for s in r.pre_arrival_steps)
        for step in r.pre_arrival_steps:
            lowered = step.lower()
            assert "balance" not in lowered
            assert "savings" not in lowered


class TestInvestor:
    def test_high_budget_is_E28A(self):
        r = _call(purpose=Purpose.INVESTOR, budget_band=BudgetBand.OVER_500M)
        assert r.recommended_visa is VisaType.E28A

    def test_mid_budget_ranking_excludes_e33g(self):
        r = _call(purpose=Purpose.INVESTOR, budget_band=BudgetBand.MID_50_500M)
        visas = {rv.visa for rv in r.ranking}
        assert VisaType.E33G not in visas
        assert VisaType.D12 in visas or VisaType.E28A in visas

    def test_under_budget_refers(self):
        r = _call(purpose=Purpose.INVESTOR, budget_band=BudgetBand.UNDER_50M)
        assert r.recommended_visa is None
        assert r.referral_mode is True

    def test_under_budget_referral_names_no_visa_code_and_no_savings(self):
        r = _call(purpose=Purpose.INVESTOR, budget_band=BudgetBand.UNDER_50M)
        assert r.referral_mode is True
        lowered = r.reason.lower()
        assert "e33g" not in lowered
        assert "savings" not in lowered
        assert "whatsapp" in lowered


class TestSimpleBranches:
    def test_work_employee_is_E23(self):
        r = _call(purpose=Purpose.WORK_EMPLOYEE, duration_months=12)
        assert r.recommended_visa is VisaType.E23

    def test_work_employee_short_can_suggest_c18(self):
        r = _call(purpose=Purpose.WORK_EMPLOYEE, duration_months=3)
        assert r.recommended_visa in {VisaType.E23, VisaType.C18, VisaType.C22A}

    def test_family_is_E31(self):
        assert _call(purpose=Purpose.FAMILY).recommended_visa is VisaType.E31

    def test_retirement_standard_is_E33F(self):
        r = _call(purpose=Purpose.RETIREMENT, budget_band=BudgetBand.MID_50_500M)
        assert r.recommended_visa is VisaType.E33F

    def test_retirement_high_budget_surfaces_E33E(self):
        r = _call(purpose=Purpose.RETIREMENT, budget_band=BudgetBand.OVER_500M)
        visas = {rv.visa for rv in r.ranking}
        assert VisaType.E33E in visas

    def test_student_is_E30A(self):
        assert _call(purpose=Purpose.STUDENT).recommended_visa is VisaType.E30A


class TestPreArrivalSteps:
    def test_all_branches_with_recommendation_return_steps(self):
        scenarios = [
            (Purpose.LONG_TOURISM, 1, BudgetBand.MID_50_500M),
            (Purpose.WORK_REMOTE, 12, BudgetBand.OVER_500M),
            (Purpose.INVESTOR, 24, BudgetBand.OVER_500M),
            (Purpose.WORK_EMPLOYEE, 12, BudgetBand.MID_50_500M),
            (Purpose.FAMILY, 12, BudgetBand.UNDER_50M),
            (Purpose.RETIREMENT, 12, BudgetBand.MID_50_500M),
            (Purpose.STUDENT, 12, BudgetBand.UNDER_50M),
        ]
        for purpose, months, band in scenarios:
            r = _call(purpose=purpose, duration_months=months, budget_band=band)
            if r.recommended_visa is not None:
                assert len(r.pre_arrival_steps) >= 3, (
                    f"{purpose} with {band.value} should produce pre-arrival steps"
                )


class TestCoverageSweep:
    """Every purpose × budget combination must produce either a ranking or a referral."""

    def test_all_combinations_terminate(self):
        for purpose in Purpose:
            for band in BudgetBand:
                for months in (1, 6, 12, 24):
                    r = _call(purpose=purpose, duration_months=months, budget_band=band)
                    assert r.ranking or r.referral_mode, (
                        f"purpose={purpose} months={months} band={band} produced empty+no-referral"
                    )


class TestPreArrivalStepsBelongToTheRecommendedVisa:
    """A visitor's checklist must contain their route's money, and no other's.

    Every caller presents this list as ONE visa's mandatory list: the frontend
    renders it as an ordered list under "Pre-arrival checklist"
    (app/visa/match/[hash]/page.tsx), and the funnel email introduces it as
    "make sure you have the <visa> essentials"
    (notifications/funnel_email/templates.py). A purpose-level list therefore
    cannot carry a product-level figure — RETIREMENT ranks E33E and E33F, and
    only E33E requires a deposit. Stating both in one checklist tells an E33F
    client to place USD 50,000 they do not need, and shows an E33E client "no
    deposit required" immediately above a deposit.
    """

    def _steps_for_recommendation(
        self, months: int, band: BudgetBand
    ) -> tuple[VisaType, list[str]]:
        r = _call(purpose=Purpose.RETIREMENT, duration_months=months, budget_band=band)
        assert r.recommended_visa is not None
        return r.recommended_visa, r.pre_arrival_steps

    def test_e33f_client_is_never_told_to_place_a_deposit(self):
        """The defect this class exists to prevent."""
        visa, steps = self._steps_for_recommendation(12, BudgetBand.MID_50_500M)
        assert visa is VisaType.E33F
        joined = " ".join(steps)
        assert "50,000" not in joined, f"E33F client was shown E33E's deposit: {steps!r}"

    def test_e33e_client_is_told_about_the_deposit(self):
        visa, steps = self._steps_for_recommendation(60, BudgetBand.OVER_500M)
        assert visa is VisaType.E33E
        deposit = [s for s in steps if "50,000" in s]
        assert len(deposit) == 1, f"expected exactly one deposit step, got {deposit!r}"
        line = deposit[0].lower()
        assert "bumn" in line or "state-owned" in line, (
            f"the deposit is stated without naming the qualifying bank type: {line!r}"
        )
        assert "any bank" not in line and "any indonesian bank" not in line

    def test_no_checklist_both_requires_and_waives_a_deposit(self):
        """The two lines are mutually exclusive; one list must never hold both."""
        for months, band in ((12, BudgetBand.MID_50_500M), (60, BudgetBand.OVER_500M)):
            _, steps = self._steps_for_recommendation(months, band)
            joined = " ".join(steps).lower()
            waives = "no deposit" in joined or "requires no deposit" in joined
            requires = "50,000" in joined
            assert not (waives and requires), (
                f"checklist both waives and requires a deposit: {steps!r}"
            )

    def test_both_routes_state_the_shared_income_requirement(self):
        """E33E and E33F DO share the USD 3,000/month bar — only the deposit differs."""
        for months, band in ((12, BudgetBand.MID_50_500M), (60, BudgetBand.OVER_500M)):
            _, steps = self._steps_for_recommendation(months, band)
            assert any("3,000" in s for s in steps), (
                f"the shared income requirement is missing: {steps!r}"
            )

    def test_superseded_income_figure_appears_in_no_format(self):
        """USD 1,500/month is the pre-2024 figure — a forbidden claim.

        Checked in every separator style the model might emit, not just the
        one this file happens to use.
        """
        for months, band in ((12, BudgetBand.MID_50_500M), (60, BudgetBand.OVER_500M)):
            _, steps = self._steps_for_recommendation(months, band)
            joined = " ".join(steps)
            for form in ("1,500", "1.500", "1 500", "1500"):
                assert form not in joined, f"superseded figure {form!r} in {steps!r}"

    def test_other_purposes_keep_their_steps_unchanged(self):
        """The per-visa layer must not disturb branches that never needed it."""
        for purpose in (
            Purpose.INVESTOR,
            Purpose.WORK_EMPLOYEE,
            Purpose.FAMILY,
            Purpose.STUDENT,
        ):
            r = _call(purpose=purpose, duration_months=12)
            if r.recommended_visa is not None:
                assert r.pre_arrival_steps == match_tree._STEPS_BY_PURPOSE.get(purpose, []), (
                    f"{purpose} steps changed"
                )


class TestImpresariat:
    """E23-FREELANCE is the Impresariat product (owner ruling 2026-10-19)."""

    def _activity(self, activity, purpose=Purpose.WORK_REMOTE):
        return recommend_visa(
            nationality="GBR",
            purpose=purpose,
            duration_months=6,
            budget_band=BudgetBand.MID_50_500M,
            activity=activity,
        )

    def test_dj_or_tennis_coach_with_impresario_sponsor_matches_impresariat(self):
        for purpose in (Purpose.WORK_REMOTE, Purpose.WORK_EMPLOYEE):
            r = self._activity(WorkActivity.SEMI_INDEPENDENT_ARTS_SPORTS, purpose)
            assert r.recommended_visa is VisaType.E23_FREELANCE
            assert "Impresariat KITAS" in r.reason
            assert "impresario" in r.reason.lower()
            assert any("Bali Zero as impresario company" in s for s in r.pre_arrival_steps)
            assert any("RPTKA" in s for s in r.pre_arrival_steps)

    def test_checklist_has_no_invoicing_framing(self):
        r = self._activity(WorkActivity.SEMI_INDEPENDENT_ARTS_SPORTS)
        text = " ".join(r.pre_arrival_steps + [r.reason]).lower()
        assert "invoice" not in text
        assert "npwp" not in text

    def test_freelancer_invoicing_indonesian_clients_is_not_impresariat(self):
        r = self._activity(WorkActivity.FREELANCE_INDONESIAN_CLIENTS)
        assert r.recommended_visa is None
        assert r.referral_mode is True
        assert VisaType.E23_FREELANCE not in [rv.visa for rv in r.ranking]
        assert "Pasal 34(1)(b)" in r.reason
        assert "employment agreement required" in r.reason
        assert "not for freelancers" in r.reason

    def test_freelancer_with_foreign_clients_gets_no_work_recommendation(self):
        for purpose in (Purpose.WORK_REMOTE, Purpose.WORK_EMPLOYEE):
            r = self._activity(WorkActivity.FREELANCE_FOREIGN_CLIENTS, purpose)
            assert r.recommended_visa is None
            assert r.referral_mode is True
            assert r.ranking == []
            assert not {rv.visa for rv in r.ranking} & {
                VisaType.C1,
                VisaType.C2,
                VisaType.D2,
                VisaType.E23_FREELANCE,
            }
            assert "employed by a company established outside Indonesia" in r.reason

    def test_catalogue_labels(self):
        meta = VISA_META[VisaType.E23_FREELANCE]
        assert meta.name_en == "Impresariat KITAS (E23, 180 days, impresario-sponsored)"
        assert meta.name_id.startswith("KITAS Impresariat (E23, 180 hari")
        assert meta.duration_days == 180
        assert "freelance" not in (meta.name_en + meta.name_id).lower()

    def test_impresariat_max_stay_is_180_days_and_not_extendable(self):
        meta = VISA_META[VisaType.E23_FREELANCE]
        assert meta.duration_days == 180
        assert meta.extensions == (0, 0)
        assert "temporary rptka" in meta.notes.lower()
        assert "a further stay needs a new application" in meta.notes.lower()

    def test_twelve_month_plan_gets_honest_max_180_days_reason(self):
        r = recommend_visa(
            nationality="GBR",
            purpose=Purpose.WORK_REMOTE,
            duration_months=12,
            budget_band=BudgetBand.MID_50_500M,
            activity=WorkActivity.SEMI_INDEPENDENT_ARTS_SPORTS,
        )
        assert r.recommended_visa is VisaType.E23_FREELANCE
        assert "Max stay 180 days" in r.reason
        assert "12-month plan" in r.reason

    def test_yes_ranks_impresariat_first_with_usual_alternatives_behind(self):
        for purpose in (Purpose.WORK_REMOTE, Purpose.WORK_EMPLOYEE):
            r = self._activity(WorkActivity.SEMI_INDEPENDENT_ARTS_SPORTS, purpose)
            assert r.ranking[0].visa is VisaType.E23_FREELANCE
            assert [rv.visa for rv in r.ranking].count(VisaType.E23_FREELANCE) == 1

    def test_no_or_unanswered_never_returns_impresariat(self):
        for purpose in (Purpose.WORK_REMOTE, Purpose.WORK_EMPLOYEE):
            for band in BudgetBand:
                r = recommend_visa(
                    nationality="GBR", purpose=purpose, duration_months=6, budget_band=band
                )
                assert VisaType.E23_FREELANCE not in [rv.visa for rv in r.ranking]

    def test_yes_on_a_non_work_purpose_is_ignored(self):
        r = self._activity(WorkActivity.SEMI_INDEPENDENT_ARTS_SPORTS, Purpose.LONG_TOURISM)
        assert VisaType.E23_FREELANCE not in [rv.visa for rv in r.ranking]


class TestMatchRequestField:
    def test_field_is_optional_and_defaults_to_none(self):
        from backend.app.routers.visa_check import MatchRequest

        base = {
            "nationality": "gbr",
            "purpose": "work_remote",
            "duration_months": 6,
            "budget_band": "50m_500m",
        }
        assert MatchRequest(**base).semi_independent_arts_sports is None
        req = MatchRequest(**base, semi_independent_arts_sports=True)
        assert req.semi_independent_arts_sports is True


class TestNationalitySafetyAndNormativeFixes:
    """Adversarial regression coverage for P12, P16, P18, P19, P20, P21, P23."""

    def test_indonesian_citizen_id_and_idn_refer_without_foreigner_visa(self):
        for code in ("ID", "IDN", "id", "ID,US"):
            r = recommend_visa(
                nationality=code,
                purpose=Purpose.LONG_TOURISM,
                duration_months=3,
                budget_band=BudgetBand.UNDER_50M,
            )
            assert r.recommended_visa is None, f"{code} got {r.recommended_visa}"
            assert r.referral_mode is True
            assert "Indonesian citizens" in r.reason or "WNI" in r.reason
            assert "whatsapp" in r.reason.lower()

    def test_india_ind_and_in_are_not_mistaken_for_indonesia(self):
        for code in ("IN", "IND"):
            r = recommend_visa(
                nationality=code,
                purpose=Purpose.LONG_TOURISM,
                duration_months=1,
                budget_band=BudgetBand.UNDER_50M,
            )
            assert r.referral_mode is False
            assert r.recommended_visa is VisaType.B1

    def test_calling_visa_nationalities_refer_to_advisor_never_voa_or_evisa(self):
        calling_codes = ("AF", "AFG", "IL", "ISR", "KP", "PRK", "LR", "LBR", "NG", "NGA", "SO", "SOM")
        for code in calling_codes:
            for purpose in (Purpose.LONG_TOURISM, Purpose.WORK_EMPLOYEE, Purpose.INVESTOR):
                r = recommend_visa(
                    nationality=code,
                    purpose=purpose,
                    duration_months=1,
                    budget_band=BudgetBand.OVER_500M,
                )
                assert r.recommended_visa is None, f"{code}/{purpose} got {r.recommended_visa}"
                assert r.referral_mode is True
                assert r.ranking == []
                assert "Calling Visa" in r.reason
                assert "whatsapp" in r.reason.lower()

    def test_non_voa_nationality_pk_pak_excludes_b1_and_recommends_c1(self):
        for code in ("PK", "PAK", "BD", "BGD"):
            r = recommend_visa(
                nationality=code,
                purpose=Purpose.LONG_TOURISM,
                duration_months=1,
                budget_band=BudgetBand.UNDER_50M,
            )
            assert r.referral_mode is False
            assert VisaType.B1 not in {rv.visa for rv in r.ranking}
            assert r.recommended_visa is VisaType.C1

    def test_p12_software_engineer_6m_and_24m_recommends_e23_never_c7(self):
        for months in (6, 12, 24):
            r = recommend_visa(
                nationality="IND",
                purpose=Purpose.WORK_EMPLOYEE,
                duration_months=months,
                budget_band=BudgetBand.MID_50_500M,
            )
            assert r.recommended_visa is VisaType.E23
            ranked_visas = {rv.visa for rv in r.ranking}
            assert not (ranked_visas & {VisaType.C7, VisaType.C7A, VisaType.C7B})

    def test_p23_family_steps_include_wni_ktp_kartu_keluarga_option(self):
        r = recommend_visa(
            nationality="AUS",
            purpose=Purpose.FAMILY,
            duration_months=24,
            budget_band=BudgetBand.UNDER_50M,
        )
        assert r.recommended_visa is VisaType.E31
        assert (
            r.pre_arrival_steps[0]
            == "Sponsor's KTP & Kartu Keluarga (if Indonesian citizen) or KITAS/KITAP + passport copies"
        )

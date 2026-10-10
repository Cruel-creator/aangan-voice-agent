from datetime import date, timedelta

import pytest

from app.config import get_rules
from app.qualify.area import check_service_area, resolve_locality
from app.qualify.budget import check_budget
from app.qualify.decision import Flag, LeadFacts, Outcome, decide
from app.qualify.scope import check_scope
from app.qualify.timeline import check_timeline, parse_deadline

TODAY = date(2026, 9, 3)
RULES = get_rules()


# ---------- scope ----------
@pytest.mark.parametrize("ptype", ["restaurant", "Cafe", "GYM", "hotel", "retail", "shop"])
def test_scope_declines_project_types(ptype):
    assert check_scope(ptype).result == "fail"


@pytest.mark.parametrize(
    "service", ["advice_only", "styling_only", "self_execution", "standalone_vastu", "structural_work"]
)
def test_scope_declines_service_types(service):
    assert check_scope("apartment", "residential", service).result == "fail"


def test_scope_advice_wording_without_service_asks_once():
    res = check_scope("apartment", description="just ideas on colours for my living room")
    assert res.result == "unclear"


def test_scope_commercial_bounds():
    assert check_scope("office", "commercial", area_sqft=3000).result == "pass"
    assert check_scope("office", "commercial", area_sqft=3001).result == "fail"
    assert check_scope("office", "commercial", area_sqft=500).result == "pass"
    below = check_scope("office", "commercial", area_sqft=499)
    assert below.result == "unclear" and below.needs_human_review
    assert check_scope("office", "commercial").result == "unclear"


def test_scope_residential_has_no_area_cap():
    assert check_scope("villa", "residential", area_sqft=5500).result == "pass"


def test_scope_below_min_switches_to_decline_when_configured():
    rules = RULES.model_copy(deep=True)
    rules.scope.commercial.below_min_action = "decline"
    assert check_scope("office", "commercial", area_sqft=180, rules=rules).result == "fail"


# ---------- area ----------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Kothrud", "in"),
        ("kothrud", "in"),
        ("KOREGAON PARK", "in"),
        ("KP", "in"),
        ("Pimple Saudaagar", "in"),
        ("Pimple Saudagar, Pune", "in"),
        ("near Baner", "in"),
        ("Kondhwa Budruk", "in"),
        ("Wakkad", "in"),
        ("Nashik", "out"),
        ("Talegaon Dabhade", "out"),
        ("talegaon", "out"),
        ("Navi Mumbai", "out"),
        ("Mumbai", "out"),
        ("Lonavla", "out"),
        ("Alibaug", "unclear"),
        ("", "unclear"),
        (None, "unclear"),
    ],
)
def test_area(text, expected):
    assert check_service_area(text) == expected


def test_area_returns_canonical_name():
    assert resolve_locality("Pimple Saudaagar").canonical == "Pimple Saudagar"
    assert resolve_locality("KP").canonical == "Koregaon Park"


# ---------- timeline ----------
def test_parse_month_next_occurrence_end_of_month():
    assert parse_deadline("March", TODAY) == date(2027, 3, 31)
    assert parse_deadline("November", TODAY) == date(2026, 11, 30)
    assert parse_deadline("September", TODAY) == date(2026, 9, 30)
    assert parse_deadline("by March 2027", TODAY) == date(2027, 3, 31)
    assert parse_deadline("Feb", TODAY) == date(2027, 2, 28)


def test_parse_iso_and_relative():
    assert parse_deadline("2027-01-15", TODAY) == date(2027, 1, 15)
    assert parse_deadline("2027-02", TODAY) == date(2027, 2, 28)
    assert parse_deadline("in 3 weeks", TODAY) == TODAY + timedelta(weeks=3)
    assert parse_deadline("within two months", TODAY) == date(2026, 11, 3)
    assert parse_deadline("15th March", TODAY) == date(2027, 3, 15)
    assert parse_deadline("before Diwali", TODAY) is None


def test_timeline_boundaries_exactly_10_and_14_weeks():
    ten = (TODAY + timedelta(weeks=10)).isoformat()
    just_under_ten = (TODAY + timedelta(weeks=10) - timedelta(days=1)).isoformat()
    fourteen = (TODAY + timedelta(weeks=14)).isoformat()
    just_under_fourteen = (TODAY + timedelta(weeks=14) - timedelta(days=1)).isoformat()
    assert check_timeline(just_under_ten, False, TODAY).result == "too_soon"
    assert check_timeline(ten, False, TODAY).result == "tight"
    assert check_timeline(just_under_fourteen, False, TODAY).result == "tight"
    assert check_timeline(fourteen, False, TODAY).result == "ok"


def test_timeline_missing_or_unparseable():
    assert check_timeline(None, True, TODAY).result == "ok"
    assert check_timeline(None, False, TODAY).result == "unclear"
    assert check_timeline("before Diwali", False, TODAY).result == "unclear"
    assert check_timeline("2026-01-01", False, TODAY).result == "unclear"  # past date


def test_timeline_uses_injected_today_not_clock():
    assert check_timeline("November", False, date(2026, 6, 1)).result == "ok"
    assert check_timeline("November", False, date(2026, 9, 3)).result == "tight"


# ---------- budget ----------
def test_budget_not_mentioned():
    assert check_budget(["kitchen"], "apartment", 500, None) == "not_mentioned"


def test_budget_clearly_below_uses_lower_floor():
    assert check_budget(["kitchen", "bedroom"], "apartment", 550, 100_000) == "clearly_below"
    assert check_budget(["living", "kitchen"], "apartment", 875, 400_000) == "ok"


def test_budget_ratio_boundary():
    floors = RULES.budget.floors_inr
    floor = floors.per_room  # one room, no area
    half = floor * RULES.budget.clearly_below_ratio
    assert check_budget(["kitchen"], "apartment", None, half) == "ok"
    assert check_budget(["kitchen"], "apartment", None, half - 1) == "clearly_below"


def test_budget_commercial_uses_commercial_floor():
    floors = RULES.budget.floors_inr
    area = 1000
    half = area * floors.commercial_per_sqft * RULES.budget.clearly_below_ratio
    assert check_budget(["cabin"], "office", area, half, "commercial") == "ok"
    assert check_budget(["cabin"], "office", area, half - 1, "commercial") == "clearly_below"


def test_budget_unknown_scope_is_not_judged():
    assert check_budget([], "apartment", None, 1) == "ok"


def test_budget_result_is_label_only():
    assert check_budget(["kitchen"], "apartment", 500, 10) in {"ok", "clearly_below", "not_mentioned"}


# ---------- decision ----------
def base(**kw) -> LeadFacts:
    facts = dict(
        intent="new",
        locality="Baner",
        property_type="apartment",
        project_category="residential",
        rooms=["kitchen"],
        area_sqft=900,
        service="design_and_execution",
        completion=None,
        flexible=True,
        budget_inr=None,
        decision_maker="owner",
    )
    facts.update(kw)
    return LeadFacts(**facts)


def test_complaint_escalates_without_qualifying():
    d = decide(LeadFacts(intent="existing_client_complaint", locality="Nashik"), TODAY)
    assert d.outcome == Outcome.ESCALATE_URGENT and d.criteria == {}


def test_vendor_call_not_applicable():
    assert decide(LeadFacts(intent="other"), TODAY).outcome == Outcome.NOT_APPLICABLE


def test_unknown_locality_goes_to_human_review():
    assert decide(base(locality="Alibaug"), TODAY).outcome == Outcome.HUMAN_REVIEW


def test_decision_maker_never_declines():
    d = decide(base(decision_maker="unclear"), TODAY)
    assert d.outcome == Outcome.QUALIFIED and Flag.DECISION_NOTE in d.flags
    d = decide(base(decision_maker=None), TODAY)
    assert d.outcome == Outcome.QUALIFIED and Flag.DECISION_NOTE in d.flags


def test_vip_from_referral_text():
    d = decide(base(referral_source="I'm a friend of Nikhil"), TODAY)
    assert Flag.VIP in d.flags


def test_site_available_is_not_a_deadline():
    d = decide(base(site_available="in 2 weeks", completion=None, flexible=True), TODAY)
    assert d.outcome == Outcome.QUALIFIED


def test_criteria_never_contain_figures():
    d = decide(base(budget_inr=10), TODAY)
    for value in d.criteria.values():
        assert not any(ch.isdigit() for ch in value)

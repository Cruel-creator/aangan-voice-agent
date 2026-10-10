"""Combines the five criteria into one outcome + flags. Channel-independent."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.config import Rules, get_rules
from app.qualify.area import check_service_area
from app.qualify.budget import check_budget
from app.qualify.scope import check_scope
from app.qualify.timeline import check_timeline


class Outcome(StrEnum):
    QUALIFIED = "QUALIFIED"
    DECLINE_SCOPE = "DECLINE_SCOPE"
    DECLINE_AREA = "DECLINE_AREA"
    DECLINE_TIMELINE = "DECLINE_TIMELINE"
    BUDGET_MISALIGNED = "BUDGET_MISALIGNED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ESCALATE_URGENT = "ESCALATE_URGENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Flag(StrEnum):
    TIGHT = "TIGHT"
    VIP = "VIP"
    RECOVERY = "RECOVERY"
    DECISION_NOTE = "DECISION_NOTE"
    # Conversation-level flags: set from call signals, not from project facts.
    PRICING_ASKED = "PRICING_ASKED"
    RESUMED_CALL = "RESUMED_CALL"


ENGINE_FLAGS = {Flag.TIGHT, Flag.VIP, Flag.RECOVERY, Flag.DECISION_NOTE}
CALL_FLAGS = {Flag.PRICING_ASKED, Flag.RESUMED_CALL}


class LeadFacts(BaseModel):
    """Facts extracted from a conversation (any channel)."""

    model_config = ConfigDict(extra="allow")

    intent: str | None = None  # new | follow_up | existing_client_complaint | other | unknown
    locality: str | None = None
    property_type: str | None = None
    project_category: str | None = None
    rooms: list[str] = []
    area_sqft: float | None = None
    service: str | None = None
    scope_description: str | None = None
    completion: str | None = None
    flexible: bool = False
    site_available: str | None = None  # informational only, never a deadline
    budget_inr: float | None = None
    decision_maker: str | None = None
    referral_mentions_founder: bool = False
    referral_source: str | None = None
    prior_enquiry_found: bool = False
    # Call signals
    pricing_asked: bool = False
    resumed_call: bool = False


class Decision(BaseModel):
    outcome: Outcome
    flags: list[Flag]
    criteria: dict[str, str]  # per-criterion labels, safe to store; never contains prices


QUALIFY_INTENTS = {"new", "follow_up"}


def _is_vip(facts: LeadFacts, rules: Rules) -> bool:
    if facts.referral_mentions_founder:
        return True
    text = (facts.referral_source or "").lower()
    return any(s.lower() in text for s in rules.vip.signals)


def decide(facts: LeadFacts, today: date, rules: Rules | None = None) -> Decision:
    r = rules or get_rules()
    intent = (facts.intent or "unknown").lower()

    if intent == "existing_client_complaint":
        return Decision(outcome=Outcome.ESCALATE_URGENT, flags=[], criteria={})
    if intent not in QUALIFY_INTENTS:
        return Decision(outcome=Outcome.NOT_APPLICABLE, flags=_call_flags(facts), criteria={})

    scope = check_scope(
        facts.property_type,
        facts.project_category,
        facts.service,
        facts.area_sqft,
        facts.scope_description,
        r,
    )
    area = check_service_area(facts.locality, r)
    timeline = check_timeline(facts.completion, facts.flexible, today, r)
    budget = check_budget(
        facts.rooms,
        facts.property_type,
        facts.area_sqft,
        facts.budget_inr,
        facts.project_category,
        r,
    )
    dm_category = r.decision_maker.categories.get((facts.decision_maker or "").lower(), "unclear")

    criteria = {
        "scope": scope.result,
        "scope_reason": scope.reason,
        "area": area,
        "timeline": timeline.result,
        "budget": budget,
        "decision_maker": dm_category,
    }

    # Definite declines first (a scope fail is decisive even with unknown locality).
    if scope.result == "fail":
        outcome = Outcome.DECLINE_SCOPE
    elif area == "out":
        outcome = Outcome.DECLINE_AREA
    elif timeline.result == "too_soon":
        outcome = Outcome.DECLINE_TIMELINE
    elif budget == "clearly_below":
        outcome = Outcome.BUDGET_MISALIGNED
    elif "unclear" in (scope.result, area, timeline.result):
        outcome = Outcome.HUMAN_REVIEW
    else:
        outcome = Outcome.QUALIFIED

    flags: list[Flag] = []
    if timeline.result == "tight":
        flags.append(Flag.TIGHT)
    if _is_vip(facts, r):
        flags.append(Flag.VIP)
    if intent == "follow_up" and facts.prior_enquiry_found:
        flags.append(Flag.RECOVERY)
    if dm_category in r.decision_maker.note_if:
        flags.append(Flag.DECISION_NOTE)
    flags.extend(_call_flags(facts))
    return Decision(outcome=outcome, flags=flags, criteria=criteria)


def _call_flags(facts: LeadFacts) -> list[Flag]:
    flags = []
    if facts.pricing_asked:
        flags.append(Flag.PRICING_ASKED)
    if facts.resumed_call:
        flags.append(Flag.RESUMED_CALL)
    return flags

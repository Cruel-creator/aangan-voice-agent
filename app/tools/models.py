"""Tool contract: inputs the agent sends and responses it reads.

Every response model here is agent-facing. tests/test_price_leak.py fails if any
of them can carry a price.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Service = Literal[
    "design_and_execution",
    "advice_only",
    "styling_only",
    "self_execution",
    "standalone_furniture_sourcing",
    "standalone_vastu",
    "structural_work",
]
Intent = Literal["new", "follow_up", "existing_client_complaint", "other", "unknown"]


class ToolIn(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LeadFields(ToolIn):
    intent: Intent | None = Field(None, description="Why the person called")
    name: str | None = None
    locality: str | None = Field(None, description="Area of Pune/PCMC where the site is")
    property_type: str | None = Field(
        None, description="apartment, house, villa, bungalow, office, restaurant, shop, ..."
    )
    project_category: Literal["residential", "commercial", "hospitality", "retail"] | None = None
    rooms: list[str] = Field(default_factory=list, description="e.g. kitchen, living, bedroom; or full_home")
    area_sqft: float | None = Field(None, description="Carpet area in square feet")
    service: Service | None = None
    scope_description: str | None = Field(None, description="Caller's own words")
    completion_date: str | None = Field(
        None,
        description="When it must be FINISHED: ISO date or caller's words. Not the possession/move-out date.",
    )
    flexible: bool = Field(False, description="True if the caller has no fixed deadline")
    site_available: str | None = Field(None, description="Possession / site access date")
    budget_inr: float | None = Field(
        None, description="Only if the caller volunteered a budget; their lower figure in rupees"
    )
    decision_maker: (
        Literal[
            "owner",
            "authorised_representative",
            "representative_only",
            "representative_owners_attending",
            "unclear",
        ]
        | None
    ) = None
    referral_source: str | None = None
    referral_mentions_founder: bool = False
    pricing_asked: bool = False


# ---------- inputs ----------
class LookupCallerIn(ToolIn):
    phone: str | None = None


class CheckScopeIn(ToolIn):
    scope_description: str | None = None
    property_type: str | None = None
    area_sqft: float | None = None
    project_category: str | None = None
    service: Service | None = None


class CheckServiceAreaIn(ToolIn):
    locality: str | None = None


class CheckTimelineIn(ToolIn):
    completion_date: str | None = None
    flexible: bool = False


class CheckBudgetIn(ToolIn):
    rooms: list[str] = Field(default_factory=list)
    property_type: str | None = None
    area_sqft: float | None = None
    budget_inr: float | None = None
    project_category: str | None = None


class GetSlotsIn(ToolIn):
    consult_type: Literal["site", "studio"] = "studio"
    locality: str | None = None
    project_type: Literal["residential", "commercial"] | None = None


class BookConsultationIn(LeadFields):
    slot: str = Field(description="ISO start time exactly as returned by get_slots")
    designer_id: str
    consult_type: Literal["site", "studio"] = "studio"


class SendHandoffIn(LeadFields):
    notes: str | None = Field(None, description="Anything uncertain, e.g. decision-maker")


class EscalateUrgentIn(ToolIn):
    phone: str | None = None
    name: str | None = None
    summary: str


class RouteHumanReviewIn(LeadFields):
    open_question: str


class LogCallIn(LeadFields):
    summary: str | None = None


# ---------- responses (agent-facing) ----------
class PriorEnquiry(BaseModel):
    locality: str | None
    property_type: str | None
    status: str
    enquired_on: str | None


class LookupCallerOut(BaseModel):
    existing_client: bool
    prior_enquiry: PriorEnquiry | None
    recent_dropped_call: bool
    office_hours_now: bool


class CheckScopeOut(BaseModel):
    result: Literal["pass", "fail", "unclear"]
    reason: str


class CheckServiceAreaOut(BaseModel):
    result: Literal["in", "out", "unclear"]


class CheckTimelineOut(BaseModel):
    result: Literal["ok", "tight", "too_soon", "unclear"]


class CheckBudgetOut(BaseModel):
    result: Literal["ok", "clearly_below", "not_mentioned"]


class SlotOut(BaseModel):
    start: str
    label: str
    designer_id: str


class GetSlotsOut(BaseModel):
    slots: list[SlotOut]


class BookConsultationOut(BaseModel):
    confirmed: bool
    booking_id: str | None = None
    label: str | None = None
    reason: str | None = None


class SendHandoffOut(BaseModel):
    sent: bool


class EscalateUrgentOut(BaseModel):
    escalated: bool


class LoggedOut(BaseModel):
    logged: bool


RESPONSE_MODELS = [
    LookupCallerOut,
    PriorEnquiry,
    CheckScopeOut,
    CheckServiceAreaOut,
    CheckTimelineOut,
    CheckBudgetOut,
    SlotOut,
    GetSlotsOut,
    BookConsultationOut,
    SendHandoffOut,
    EscalateUrgentOut,
    LoggedOut,
]

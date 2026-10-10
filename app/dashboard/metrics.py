"""Dashboard numbers. Every metric is None when there is no data for it, never a guess."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from statistics import median

from app.services import cost
from app.services.crm import CRM

DECLINES = ["DECLINE_SCOPE", "DECLINE_AREA", "DECLINE_TIMELINE", "BUDGET_MISALIGNED"]
ENQUIRY_INTENTS = {"new", "follow_up"}


def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def window_calls(crm: CRM, start: datetime, end: datetime, include_demo: bool = False):
    calls = crm.calls_between(start, end)
    return calls if include_demo else [c for c in calls if not c.is_demo]


def recent_calls(
    crm: CRM, start: datetime, end: datetime, include_demo: bool = False, limit: int = 50
) -> list[dict]:
    """Newest first. Only fields that are safe on a public page (no phone data)."""
    calls = sorted(
        window_calls(crm, start, end, include_demo),
        key=lambda c: c.started_at.timestamp() if c.started_at else 0,
        reverse=True,
    )
    return [
        {
            "id": c.id,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "outcome": c.outcome,
            "intent": c.intent,
            "flags": c.flags or [],
            "duration_seconds": c.duration_seconds,
            "is_demo": bool(c.is_demo),
            "price_leak": bool(c.price_leak),
        }
        for c in calls[:limit]
    ]


def compute(crm: CRM, start: datetime, end: datetime, include_demo: bool = False) -> dict:
    calls = window_calls(crm, start, end, include_demo)
    call_ids = [c.id for c in calls if c.id]
    leads = crm.leads_for_calls(call_ids)
    lead_ids = [lead.id for lead in leads if lead.id]
    handoffs = crm.handoffs_for_leads(lead_ids)
    outcomes = {o.lead_id: o for o in crm.outcomes_for_leads(lead_ids)}
    audits = crm.audits_between(start, end)

    pickups = [c.pickup_seconds for c in calls if c.pickup_seconds is not None]
    office_known = [c for c in calls if c.in_office_hours is not None]
    ack_minutes = [(h.acked_at - h.sent_at).total_seconds() / 60 for h in handoffs if h.acked_at]

    enquiries = [c for c in calls if c.intent in ENQUIRY_INTENTS]
    qualified = [c for c in calls if c.outcome == "QUALIFIED"]
    booked = [lead for lead in leads if lead.booking_id]
    held = [lead for lead in booked if outcomes.get(lead.id) and outcomes[lead.id].held]
    won_outcomes = [o for o in outcomes.values() if o.won]
    decided = [o for o in outcomes.values() if o.won is not None]

    days = max((end - start).total_seconds() / 86400, 1)
    costs = cost.summarise([c.cost_inr or 0.0 for c in calls], months=days / 30)
    win_rate = len(won_outcomes) / len(decided) if decided else None
    values = [o.project_value_inr for o in won_outcomes if o.project_value_inr]
    avg_value = sum(values) / len(values) if values else None
    pipeline = len(booked) * win_rate * avg_value if win_rate is not None and avg_value is not None else None

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "speed": {
            "pct_answered_under_5_min": _pct(sum(p <= 300 for p in pickups), len(pickups)),
            "median_pickup_seconds": median(pickups) if pickups else None,
            "after_hours_share_pct": _pct(
                sum(not c.in_office_hours for c in office_known), len(office_known)
            ),
            "median_minutes_to_designer_ack": round(median(ack_minutes), 1) if ack_minutes else None,
        },
        "funnel": {
            "calls": len(calls),
            "enquiries": len(enquiries),
            "qualified": len(qualified),
            "booked": len(booked),
            "held": len(held),
            "won": sum(1 for lead in booked if outcomes.get(lead.id) and outcomes[lead.id].won),
        },
        "declines": {k: v for k, v in Counter(c.outcome for c in calls).items() if k in DECLINES},
        "quality": {
            "human_review_rate_pct": _pct(
                sum(c.outcome == "HUMAN_REVIEW" for c in enquiries), len(enquiries)
            ),
            "escalations": sum(c.outcome == "ESCALATE_URGENT" for c in calls),
            "transfers": sum(
                1
                for c in calls
                if c.ended_reason and ("transfer" in c.ended_reason or "forward" in c.ended_reason)
            ),
            "price_leak_flags": sum(bool(c.price_leak) for c in calls),
            "unlogged_calls": sum(c.outcome is None for c in calls),
            "audit_agreement_pct": _pct(sum(a.agrees for a in audits), len(audits)),
            "audits": len(audits),
        },
        "cost": {
            "total_inr": costs.total_inr,
            "monthly_inr": round(costs.total_inr / (days / 30), 2),
            "per_call_inr": costs.per_call_inr,
            "per_booked_consultation_inr": costs.per(len(booked)),
            "win_rate_pct": round(win_rate * 100, 1) if win_rate is not None else None,
            "avg_project_value_inr": round(avg_value) if avg_value is not None else None,
            "pipeline_value_inr": round(pipeline) if pipeline is not None else None,
        },
    }

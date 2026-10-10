"""One thin handler per agent tool: validate → call qualify/ or services/ → contract response."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel

from app.config import get_rules, get_settings
from app.platform.webhooks import CallContext
from app.qualify import area, budget, scope, timeline
from app.qualify.decision import Decision, LeadFacts, Outcome, decide
from app.services import calendar, clock, notify, sms, telegram
from app.services.crm import (
    CallRecord,
    Designer,
    Lead,
    decrypt_phone,
    encrypt_phone,
    get_crm,
    hash_phone,
    normalize_phone,
)
from app.tools import models as m

log = logging.getLogger(__name__)

LEAD_COLUMNS = {
    "name",
    "locality",
    "property_type",
    "project_category",
    "rooms",
    "area_sqft",
    "service",
    "budget_inr",
    "decision_maker",
    "referral_source",
}
STATUS_BY_OUTCOME = {
    Outcome.DECLINE_SCOPE: "declined",
    Outcome.DECLINE_AREA: "declined",
    Outcome.DECLINE_TIMELINE: "re_engage",
    Outcome.BUDGET_MISALIGNED: "declined_budget",
    Outcome.HUMAN_REVIEW: "human_review",
}


# ---------- shared helpers ----------
def _ensure_call(ctx: CallContext, phone: str | None = None) -> CallRecord:
    number = ctx.caller_number or phone
    record = CallRecord(platform_call_id=ctx.call_id, is_demo=True if ctx.demo else None)
    if number:
        record.phone_hash = hash_phone(number)
        record.phone_encrypted = encrypt_phone(number)
    return get_crm().upsert_call(record)


def _lead_for_call(call: CallRecord) -> Lead:
    crm = get_crm()
    leads = crm.leads_for_calls([call.id])
    return leads[0] if leads else crm.create_lead(Lead(call_id=call.id, created_at=clock.now()))


def _facts(fields: m.LeadFields, call: CallRecord | None = None) -> LeadFacts:
    data = fields.model_dump()
    data["completion"] = data.pop("completion_date")
    facts = LeadFacts(**data)
    if call and call.phone_hash:
        _, prior = get_crm().lookup_caller(call.phone_hash, exclude_call_id=call.id)
        facts.prior_enquiry_found = prior is not None
        if facts.intent == "new" and prior is not None:
            facts.intent = "follow_up"
        facts.resumed_call = _recent_dropped(call)
    return facts


def _recent_dropped(call: CallRecord) -> bool:
    if not call.phone_hash:
        return False
    window = timedelta(minutes=get_rules().routing.dropped_call_resume_minutes)
    now = clock.now()
    for other in get_crm().calls_by_phone_hash(call.phone_hash):
        if other.id != call.id and other.ended_at and now - other.ended_at <= window:
            return True
    return False


def _save_lead(lead: Lead, fields: m.LeadFields, decision: Decision | None = None, **extra) -> Lead:
    updates = {k: v for k, v in fields.model_dump().items() if k in LEAD_COLUMNS and v}
    if fields.completion_date:
        updates["completion"] = fields.completion_date
    if decision:
        updates["vip"] = "VIP" in decision.flags
        updates["recovery"] = "RECOVERY" in decision.flags
    updates.update(extra)
    return get_crm().update_lead(lead.id, **updates) if updates else lead


def _record_decision(call: CallRecord, decision: Decision, intent: str | None, by_agent: bool) -> None:
    get_crm().upsert_call(
        CallRecord(
            platform_call_id=call.platform_call_id,
            intent=intent,
            outcome=decision.outcome.value,
            flags=[f.value for f in decision.flags],
            criteria=decision.criteria,
            logged_by_agent=by_agent or None,
        )
    )


def _caller_phone(call: CallRecord, ctx: CallContext) -> str | None:
    if ctx.caller_number:
        return normalize_phone(ctx.caller_number)
    if call.phone_encrypted:
        try:
            return decrypt_phone(call.phone_encrypted)
        except Exception:
            log.exception("could not decrypt caller phone")
    return None


def _link(call: CallRecord) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/dashboard/calls/{call.id}"


def _studio_head() -> Designer:
    return Designer(
        id="studio_head",
        name="Studio head",
        calendar_id="",
        telegram_chat_id=get_settings().telegram_studio_head_chat_id or None,
        email=get_settings().studio_head_email or None,
    )


def _pick_designer(lead: Lead, locality: str | None, commercial: bool) -> Designer:
    crm = get_crm()
    if lead.designer_id and (d := crm.get_designer(lead.designer_id)):
        return d
    eligible = calendar.eligible_designers(crm.list_designers(), locality, commercial)
    return eligible[0] if eligible else _studio_head()


# ---------- tools ----------
def lookup_caller(args: m.LookupCallerIn, ctx: CallContext) -> m.LookupCallerOut:
    call = _ensure_call(ctx, args.phone)
    existing, prior = (False, None)
    if call.phone_hash:
        existing, prior = get_crm().lookup_caller(call.phone_hash, exclude_call_id=call.id)
    return m.LookupCallerOut(
        existing_client=existing,
        prior_enquiry=m.PriorEnquiry(
            locality=prior.locality,
            property_type=prior.property_type,
            status=prior.status,
            enquired_on=prior.created_at.date().isoformat() if prior.created_at else None,
        )
        if prior
        else None,
        recent_dropped_call=_recent_dropped(call),
        office_hours_now=clock.in_office_hours(clock.now()),
    )


def check_scope(args: m.CheckScopeIn, ctx: CallContext) -> m.CheckScopeOut:
    res = scope.check_scope(
        args.property_type, args.project_category, args.service, args.area_sqft, args.scope_description
    )
    return m.CheckScopeOut(result=res.result, reason=res.reason)


def check_service_area(args: m.CheckServiceAreaIn, ctx: CallContext) -> m.CheckServiceAreaOut:
    return m.CheckServiceAreaOut(result=area.check_service_area(args.locality))


def check_timeline(args: m.CheckTimelineIn, ctx: CallContext) -> m.CheckTimelineOut:
    res = timeline.check_timeline(args.completion_date, args.flexible, clock.now().date())
    return m.CheckTimelineOut(result=res.result)


def check_budget(args: m.CheckBudgetIn, ctx: CallContext) -> m.CheckBudgetOut:
    return m.CheckBudgetOut(
        result=budget.check_budget(
            args.rooms, args.property_type, args.area_sqft, args.budget_inr, args.project_category
        )
    )


def get_slots(args: m.GetSlotsIn, ctx: CallContext) -> m.GetSlotsOut:
    commercial = args.project_type == "commercial"
    pool = calendar.demo_designers() if ctx.demo else get_crm().list_designers()
    designers = calendar.eligible_designers(pool, args.locality, commercial)
    gateway = calendar.DemoCalendar() if ctx.demo else calendar.get_calendar()
    slots = calendar.find_slots(gateway, designers, args.consult_type, clock.now())
    return m.GetSlotsOut(
        slots=[m.SlotOut(start=s.start.isoformat(), label=s.label, designer_id=s.designer_id) for s in slots]
    )


def book_consultation(args: m.BookConsultationIn, ctx: CallContext) -> m.BookConsultationOut:
    try:
        start = datetime.fromisoformat(args.slot.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=clock.tz())
        start = start.astimezone(clock.tz())
        if start <= clock.now():
            return m.BookConsultationOut(confirmed=False, reason="slot_in_past")
        crm = get_crm()
        if ctx.demo:
            designer = next((d for d in calendar.demo_designers() if d.id == args.designer_id), None)
        else:
            designer = crm.get_designer(args.designer_id)
        if not designer or not designer.active:
            return m.BookConsultationOut(confirmed=False, reason="unknown_designer")
        call = _ensure_call(ctx)
        lead = _lead_for_call(call)
        summary = f"Aangan consultation ({args.consult_type}) - {args.name or 'new client'}"
        description = (
            f"Locality: {args.locality or '-'}\nRooms: {', '.join(args.rooms) or '-'}\nDetails: {_link(call)}"
        )
        gateway = calendar.DemoCalendar() if ctx.demo else calendar.get_calendar()
        result = calendar.book(gateway, designer, start, args.consult_type, summary, description)
        if not result.confirmed:
            return m.BookConsultationOut(confirmed=False, reason=result.reason)

        _save_lead(
            lead,
            args,
            designer_id=None if ctx.demo else designer.id,  # demo designer is not a real row
            booking_id=result.booking_id,
            consult_at=start,
            consult_type=args.consult_type,
            status="booked",
        )
        label = calendar.Slot(start, start, designer.id).label
        if ctx.demo:
            return m.BookConsultationOut(confirmed=True, booking_id=result.booking_id, label=label)
        crm.touch_designer(designer.id, clock.now())
        phone = _caller_phone(call, ctx)
        if phone:
            try:
                sms.get_sms().send(
                    phone, sms.confirmation_text(label, args.consult_type, get_settings().studio_address)
                )
            except Exception:
                log.exception("confirmation SMS failed (booking stands)")
        return m.BookConsultationOut(confirmed=True, booking_id=result.booking_id, label=label)
    except Exception:
        log.exception("book_consultation failed")
        return m.BookConsultationOut(confirmed=False, reason="booking_error")


def send_handoff(args: m.SendHandoffIn, ctx: CallContext) -> m.SendHandoffOut:
    try:
        call = _ensure_call(ctx)
        lead = _lead_for_call(call)
        facts = _facts(args, call)
        decision = decide(facts, clock.now().date())
        lead = _save_lead(
            lead,
            args,
            decision,
            open_question=args.notes or lead.open_question,
            status=lead.status if lead.status == "booked" else "qualified",
        )
        _record_decision(call, decision, facts.intent, by_agent=False)
        if ctx.demo:
            return m.SendHandoffOut(sent=True)  # demo: recorded, but no designer is messaged
        designer = _pick_designer(
            lead, args.locality, scope.is_commercial(args.property_type, args.project_category)
        )
        notify.send_handoff(
            get_crm(),
            designer,
            lead,
            [f.value for f in decision.flags],
            _caller_phone(call, ctx),
            _link(call),
            clock.now(),
        )
        return m.SendHandoffOut(sent=True)
    except Exception:
        log.exception("send_handoff failed")
        return m.SendHandoffOut(sent=False)


def escalate_urgent(args: m.EscalateUrgentIn, ctx: CallContext) -> m.EscalateUrgentOut:
    try:
        call = _ensure_call(ctx, args.phone)
        get_crm().upsert_call(
            CallRecord(
                platform_call_id=call.platform_call_id,
                intent="existing_client_complaint",
                outcome=Outcome.ESCALATE_URGENT.value,
            )
        )
        if ctx.demo:
            return m.EscalateUrgentOut(escalated=True)  # demo: recorded, nobody is paged
        phone = _caller_phone(call, ctx) or (normalize_phone(args.phone) if args.phone else None)
        telegram.escalate_urgent(phone, args.name, args.summary, _link(call))
        return m.EscalateUrgentOut(escalated=True)
    except Exception:
        log.exception("escalate_urgent failed")
        try:
            telegram.alert_front_desk("URGENT escalation failed to send - check dashboard now.")
        except Exception:
            log.exception("front desk alert also failed")
        return m.EscalateUrgentOut(escalated=False)


def route_human_review(args: m.RouteHumanReviewIn, ctx: CallContext) -> m.LoggedOut:
    try:
        call = _ensure_call(ctx)
        lead = _lead_for_call(call)
        facts = _facts(args, call)
        decision = decide(facts, clock.now().date())
        lead = _save_lead(lead, args, decision, open_question=args.open_question, status="human_review")
        get_crm().upsert_call(
            CallRecord(
                platform_call_id=call.platform_call_id,
                intent=facts.intent,
                outcome=Outcome.HUMAN_REVIEW.value,
                flags=[f.value for f in decision.flags],
                criteria=decision.criteria,
            )
        )
        if ctx.demo:
            return m.LoggedOut(logged=True)  # demo: recorded, the studio head is not messaged
        notify.send_handoff(
            get_crm(),
            _studio_head(),
            lead,
            ["HUMAN_REVIEW"],
            _caller_phone(call, ctx),
            _link(call),
            clock.now(),
        )
        return m.LoggedOut(logged=True)
    except Exception:
        log.exception("route_human_review failed")
        return m.LoggedOut(logged=False)


def log_call(args: m.LogCallIn, ctx: CallContext) -> m.LoggedOut:
    try:
        call = _ensure_call(ctx)
        facts = _facts(args, call)
        decision = decide(facts, clock.now().date())
        current = get_crm().get_call(call.id)
        if current and current.outcome in {Outcome.ESCALATE_URGENT, Outcome.HUMAN_REVIEW}:
            # A human route already chosen in this call stands.
            decision = decision.model_copy(update={"outcome": Outcome(current.outcome)})
        _record_decision(call, decision, facts.intent, by_agent=True)
        if args.summary:
            get_crm().upsert_call(CallRecord(platform_call_id=call.platform_call_id, summary=args.summary))
        if facts.intent in {"new", "follow_up"}:
            lead = _lead_for_call(call)
            status = STATUS_BY_OUTCOME.get(decision.outcome)
            if lead.status == "booked":
                status = None
            _save_lead(lead, args, decision, **({"status": status} if status else {}))
        return m.LoggedOut(logged=True)
    except Exception:
        log.exception("log_call failed")
        return m.LoggedOut(logged=False)


Handler = Callable[[BaseModel, CallContext], BaseModel]
TOOLS: dict[str, tuple[type[BaseModel], Handler]] = {
    "lookup_caller": (m.LookupCallerIn, lookup_caller),
    "check_scope": (m.CheckScopeIn, check_scope),
    "check_service_area": (m.CheckServiceAreaIn, check_service_area),
    "check_timeline": (m.CheckTimelineIn, check_timeline),
    "check_budget": (m.CheckBudgetIn, check_budget),
    "get_slots": (m.GetSlotsIn, get_slots),
    "book_consultation": (m.BookConsultationIn, book_consultation),
    "send_handoff": (m.SendHandoffIn, send_handoff),
    "escalate_urgent": (m.EscalateUrgentIn, escalate_urgent),
    "route_human_review": (m.RouteHumanReviewIn, route_human_review),
    "log_call": (m.LogCallIn, log_call),
}

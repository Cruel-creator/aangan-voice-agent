"""Server-side work that does not depend on the agent: end-of-call logging,
handoff re-routing and post-consultation follow-ups."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from app.config import get_rules, get_settings
from app.platform.webhooks import EndOfCallReport
from app.services import clock, cost, telegram
from app.services.crm import CRM, CallRecord, decrypt_phone, encrypt_phone, hash_phone
from app.services.price_audit import agent_lines, find_price_mentions

log = logging.getLogger(__name__)

# English + Hindi (Devanagari and romanised) ways of asking about price.
PRICING_ASK = re.compile(
    r"(\b(price|pricing|cost|costs|rate|rates|charges?|ballpark|how much|per sq|range|starting from"
    r"|kitna|kitne|kitni|kharcha|kharch|daam|dam|keemat|kimat)\b"
    r"|कितना|कितने|कितनी|खर्च|दाम|कीमत|रेट)",
    re.I,
)


def process_end_of_call(crm: CRM, report: EndOfCallReport) -> CallRecord:
    """Log the call even if the agent never called log_call; audit for price leaks;
    alert the front desk if a qualified call has no handoff."""
    duration = None
    if report.started_at and report.ended_at:
        duration = (report.ended_at - report.started_at).total_seconds()
    messages = [m.model_dump() for m in report.messages]
    leaks = find_price_mentions(agent_lines(messages, report.transcript))
    ended_reason = report.ended_reason or ("transferred" if report.transferred else None)

    record = CallRecord(
        platform_call_id=report.call_id,
        started_at=report.started_at,
        ended_at=report.ended_at,
        in_office_hours=clock.in_office_hours(report.started_at) if report.started_at else None,
        pickup_seconds=report.pickup_seconds,
        duration_seconds=duration,
        ended_reason=ended_reason,
        transcript=report.transcript or _render_transcript(report),
        summary=report.summary,
        platform_cost_usd=report.platform_cost_usd,
        cost_inr=cost.variable_cost_inr(report.platform_cost_usd, duration, report.platform_cost_inr),
        price_leak=bool(leaks),
    )
    if report.caller_number and get_settings().phone_hash_key:
        record.phone_hash = hash_phone(report.caller_number)
        record.phone_encrypted = encrypt_phone(report.caller_number)
    call = crm.upsert_call(record)
    call = crm.upsert_call(
        CallRecord(
            platform_call_id=call.platform_call_id,
            transcript_url=f"{get_settings().public_base_url.rstrip('/')}/dashboard/calls/{call.id}",
        )
    )

    flags = set(call.flags or [])
    if any(PRICING_ASK.search(m.text) for m in report.messages if m.role == "caller"):
        flags.add("PRICING_ASKED")
    if leaks:
        flags.add("PRICE_LEAK")
    if flags != set(call.flags or []):
        call = crm.upsert_call(CallRecord(platform_call_id=call.platform_call_id, flags=sorted(flags)))

    leads = crm.leads_for_calls([call.id])
    booked = any(lead.status == "booked" for lead in leads)
    handed_off = bool(crm.handoffs_for_leads([lead.id for lead in leads if lead.id]))
    if (call.outcome == "QUALIFIED" or booked) and not handed_off:
        telegram.alert_front_desk(
            f"Qualified call with NO designer handoff - please follow up.\nCall: {call.transcript_url}"
        )
    if leaks:
        telegram.alert_front_desk(f"Possible price mention by the AI - review: {call.transcript_url}")
    if call.outcome is None:
        log.warning("call %s ended without log_call", call.platform_call_id)
    return call


def _render_transcript(report: EndOfCallReport) -> str | None:
    if not report.messages:
        return None
    return "\n".join(f"{'AI' if m.role == 'agent' else 'Caller'}: {m.text}" for m in report.messages)


def reroute_unacked_handoffs(crm: CRM, now: datetime) -> int:
    hours = get_rules().routing.handoff_ack_reroute_hours
    count = 0
    for handoff in crm.unacked_handoffs(now - timedelta(hours=hours)):
        lead = crm.get_lead(handoff.lead_id)
        call = crm.get_call(lead.call_id) if lead and lead.call_id else None
        phone = None
        if call and call.phone_encrypted:
            try:
                phone = decrypt_phone(call.phone_encrypted)
            except Exception:
                log.exception("decrypt failed")
        try:
            telegram.get_telegram().send(
                get_settings().telegram_studio_head_chat_id,
                telegram.handoff_text(
                    lead,
                    [f"NOT ACKNOWLEDGED IN {hours:g}H"],
                    phone,
                    call.transcript_url if call else "-",
                    prefix="RE-ROUTED: ",
                )
                if lead
                else f"RE-ROUTED: handoff {handoff.id} unacknowledged (lead missing)",
            )
        except Exception:
            log.exception("re-route send failed for handoff %s", handoff.id)
            continue
        crm.mark_rerouted(handoff.id, now)
        count += 1
    return count


def send_consult_followups(crm: CRM, now: datetime) -> int:
    count = 0
    for lead in crm.leads_due_followup(now):
        designer = crm.get_designer(lead.designer_id) if lead.designer_id else None
        if not designer:
            continue
        try:
            telegram.send_consult_followup(designer, lead)
        except Exception:
            log.exception("follow-up send failed for lead %s", lead.id)
            continue
        crm.update_lead(lead.id, followup_sent_at=now)
        count += 1
    return count

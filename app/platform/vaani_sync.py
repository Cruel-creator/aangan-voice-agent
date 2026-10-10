"""Pulls finished Vaani calls into the dashboard.

Vaani's webhooks are not available on the current plan, so the backend reads Vaani's API
instead (GET /api/call-history: transcript, summary, cost, extracted entities and the
outcome tag of every call). It runs whenever the dashboard is opened (throttled) and from
the scheduled job.

For each finished call of our agent:
1. the call is logged like any platform's end-of-call report (price-leak audit included);
2. the facts Riya extracted are re-run through the qualification engine in app/qualify,
   so the outcome on the dashboard is decided in code, with the budget check the agent
   never sees. If Riya's own outcome tag disagrees, the call is flagged AGENT_MISMATCH;
3. a lead is stored, and the studio head gets a private call-back request (Telegram
   and/or email) with the caller's number, which never appears on the public dashboard.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.platform.webhooks import EndOfCallReport, TranscriptMessage
from app.qualify.decision import LeadFacts, Outcome, decide
from app.services import clock, jobs, notify
from app.services.crm import CRM, CallRecord, Lead

log = logging.getLogger(__name__)
API = "https://api.vaanivoice.ai"

# Riya's outcome tag (Analysis > Evaluations > call_outcome) -> the engine's outcome.
DISPOSITIONS = {
    "CONSULT_REQUESTED": Outcome.QUALIFIED,
    "DECLINED_SCOPE": Outcome.DECLINE_SCOPE,
    "DECLINED_AREA": Outcome.DECLINE_AREA,
    "DECLINED_TIMELINE": Outcome.DECLINE_TIMELINE,
    "HUMAN_REVIEW": Outcome.HUMAN_REVIEW,
    "URGENT_COMPLAINT": Outcome.ESCALATE_URGENT,
    "MESSAGE_TAKEN": Outcome.NOT_APPLICABLE,
    "INCOMPLETE": None,
}
INTENTS = {
    "new_project": "new",
    "new": "new",
    "follow_up": "follow_up",
    "existing_client_complaint": "existing_client_complaint",
    "vendor_or_other": "other",
}
SERVICES = {
    "design_and_execution": "design_and_execution",
    "advice_only": "advice_only",
    "styling_only": "styling_only",
    "self_execution": "self_execution",
    "furniture_only": "standalone_furniture_sourcing",
    "vastu_only": "standalone_vastu",
    "structural": "structural_work",
}
DECISION_MAKERS = {"owner": "owner", "representative": "representative_only", "unclear": "unclear"}
NEEDS_CALLBACK = {Outcome.QUALIFIED, Outcome.HUMAN_REVIEW, Outcome.ESCALATE_URGENT, Outcome.NOT_APPLICABLE}
# Riya promised these callers a call back, whatever the engine decides afterwards.
PROMISED_CALLBACK = {"CONSULT_REQUESTED", "HUMAN_REVIEW", "URGENT_COMPLAINT", "MESSAGE_TAKEN"}
UNAVAILABLE = ("not available", "not processed", "not yet", "unavailable", "not generated")


class VaaniApi:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(
            base_url=API, timeout=15, transport=transport, headers={"X-API-Key": api_key}
        )

    def call_history(self, page: int = 1, page_size: int = 50) -> dict:
        resp = self.http.get("/api/call-history", params={"page": page, "page_size": page_size})
        resp.raise_for_status()
        return resp.json()

    def trigger_call(self, agent_id: str, number: str) -> dict:
        resp = self.http.post("/api/trigger-call/", json={"agent_id": agent_id, "contact_number": number})
        resp.raise_for_status()
        return resp.json()


_api: VaaniApi | None = None


def get_api() -> VaaniApi:
    global _api
    if _api is None:
        _api = VaaniApi(get_settings().vaani_api_key)
    return _api


def set_api(api: VaaniApi | None) -> None:
    global _api
    _api = api


# ---------- parsing Vaani's fields (shapes vary by agent config; be tolerant) ----------
BLANKS = {"NA", "N/A", "NULL", "NONE", "UNCLEAR", "UNKNOWN", "NOT ENOUGH DATA"}


def _blank(v: Any) -> bool:
    """Vaani writes "unclear" or "not enough data" when it couldn't extract a value."""
    return v is None or (isinstance(v, str) and (not v.strip() or v.strip().upper() in BLANKS))


def _as_dict(v: Any) -> dict:
    """Entities / tags arrive as a dict, a JSON string, or a list of {name, value}."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    if isinstance(v, list):
        out = {}
        for item in v:
            if isinstance(item, dict):
                key = item.get("name") or item.get("key") or item.get("label")
                if key:
                    out[str(key)] = item.get("value", item.get("output"))
        return out
    return v if isinstance(v, dict) else {}


def _text(v: Any) -> str | None:
    if isinstance(v, dict):
        v = v.get("value", v.get("output"))
    return None if _blank(v) else str(v).strip()


def _time(v: Any) -> datetime | None:
    if _blank(v):
        return None
    try:
        if isinstance(v, int | float):
            dt = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=UTC)
        else:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _number(v: Any) -> float | None:
    if isinstance(v, int | float):
        return float(v)
    m = re.search(r"\d[\d,]*\.?\d*", str(v or ""))
    return float(m.group().replace(",", "")) if m else None


def budget_inr(v: Any) -> float | None:
    """Caller-volunteered budget in words: "20 lakh", "1.5 crore", "8 lakhs", "500000"."""
    text = (_text(v) or "").lower()
    n = _number(text)
    if n is None:
        return None
    if re.search(r"\b(cr|crore|crores)\b", text):
        return n * 1e7
    if re.search(r"\b(l|lac|lacs|lakh|lakhs)\b", text):
        return n * 1e5
    if re.search(r"\d\s*k\b|\bthousand\b", text):
        return n * 1e3
    return n


def _yes(v: Any) -> bool:
    return (_text(v) or "").lower() in {"yes", "true", "y", "haan", "1"}


def disposition(call: dict) -> str | None:
    for source in (call.get("call_eval_tag"), call.get("call_conversation_quality")):
        tags = _as_dict(source)
        values = list(tags.values()) if tags else [source]
        for value in values:
            text = (_text(value) or "").upper().strip()
            if text in DISPOSITIONS:
                return text
    return None


def _messages(transcript: str | None) -> list[TranscriptMessage]:
    out = []
    for line in (transcript or "").splitlines():
        speaker, sep, said = line.partition(":")
        if not sep or not said.strip():
            continue
        role = speaker.strip().lower()
        if role in {"agent", "ai", "assistant", "bot"}:
            out.append(TranscriptMessage(role="agent", text=said.strip()))
        elif role in {"user", "caller", "customer", "human"}:
            out.append(TranscriptMessage(role="caller", text=said.strip()))
    return out


def facts_from(entities: dict, tag: str | None) -> LeadFacts:
    intent = INTENTS.get((_text(entities.get("intent")) or "").lower())
    if intent is None:
        intent = {"URGENT_COMPLAINT": "existing_client_complaint", "MESSAGE_TAKEN": "other"}.get(
            tag or "", "new"
        )
    rooms = _text(entities.get("rooms"))
    return LeadFacts(
        intent=intent,
        locality=_text(entities.get("locality")),
        property_type=_text(entities.get("property_type")),
        project_category=(_text(entities.get("project_category")) or "").lower() or None,
        rooms=[r.strip() for r in re.split(r",| and ", rooms) if r.strip()] if rooms else [],
        area_sqft=_number(_text(entities.get("carpet_area_sqft"))),
        service=SERVICES.get((_text(entities.get("service_wanted")) or "").lower()),
        completion=_text(entities.get("completion_date")),
        budget_inr=budget_inr(entities.get("budget_volunteered")),
        decision_maker=DECISION_MAKERS.get((_text(entities.get("decision_maker")) or "").lower(), "unclear"),
        referral_source=_text(entities.get("referral_source")),
        referral_mentions_founder=_yes(entities.get("friend_of_nikhil")),
        pricing_asked=_yes(entities.get("pricing_asked")),
    )


def _ready(call: dict) -> bool:
    if not (call.get("End_time") or call.get("end_time")):
        return False
    status = (_text(call.get("post_processing_status")) or "").lower()
    summary = (_text(call.get("call_summary")) or "").lower()
    return status in {"completed", "complete", "done", "success", "processed"} or (
        bool(summary) and not any(u in summary for u in UNAVAILABLE)
    )


# ---------- import ----------
def import_call(crm: CRM, call: dict, today: date) -> CallRecord:
    inbound = (_text(call.get("direction")) or "inbound").lower().startswith("in")
    caller = _text(call.get("from_number") if inbound else call.get("to_number"))
    started = _time(call.get("Start_time") or call.get("start_time"))
    ended = _time(call.get("End_time") or call.get("end_time"))
    if started and not ended and _number(call.get("duration_ms")):
        ended = datetime.fromtimestamp(started.timestamp() + _number(call.get("duration_ms")) / 1000, tz=UTC)
    transcript = _text(call.get("call_transcription"))
    report = EndOfCallReport(
        call_id=str(call["call_id"]),
        caller_number=caller,
        started_at=started,
        ended_at=ended,
        ended_reason=_text(call.get("call_status")),
        platform_cost_inr=_number(call.get("call_cost")),
        summary=_text(call.get("call_summary")),
        transcript=transcript,
        messages=_messages(transcript),
    )
    record = jobs.process_end_of_call(crm, report)

    entities = _as_dict(call.get("call_entity"))
    tag = disposition(call)
    facts = facts_from(entities, tag)
    decision = decide(facts, today)
    flags = set(record.flags or []) | {f.value for f in decision.flags} | {"VAANI"}
    expected = DISPOSITIONS.get(tag or "")
    if tag and expected is not None and expected != decision.outcome:
        flags.add("AGENT_MISMATCH")  # Riya's verdict differs from the engine's: team checks
    criteria = {**decision.criteria, "agent_outcome": tag or "none"}
    record = crm.upsert_call(
        CallRecord(
            platform_call_id=record.platform_call_id,
            intent=facts.intent,
            outcome=decision.outcome.value if tag != "INCOMPLETE" else None,
            flags=sorted(flags),
            criteria=criteria,
            logged_by_agent=True,
        )
    )

    if not crm.leads_for_calls([record.id]) and facts.intent in {"new", "follow_up"}:
        preferred = _text(entities.get("preferred_day_time"))
        consult = _text(entities.get("consult_type"))
        notes = "; ".join(
            x
            for x in (
                f"Requested {consult or 'consultation'}: {preferred}" if preferred else None,
                _text(entities.get("open_question")),
            )
            if x
        )
        crm.create_lead(
            Lead(
                call_id=record.id,
                name=_text(entities.get("caller_name")),
                locality=facts.locality,
                property_type=facts.property_type,
                project_category=facts.project_category,
                rooms=facts.rooms,
                area_sqft=facts.area_sqft,
                service=facts.service,
                completion=facts.completion,
                budget_inr=facts.budget_inr,
                decision_maker=facts.decision_maker,
                referral_source=facts.referral_source,
                vip="VIP" in flags,
                status="qualified" if decision.outcome == Outcome.QUALIFIED else "human_review",
                consult_type=(consult or "").lower()
                if (consult or "").lower() in {"site", "studio"}
                else None,
                open_question=notes or None,
            )
        )

    if tag in PROMISED_CALLBACK or (decision.outcome in NEEDS_CALLBACK and tag != "INCOMPLETE"):
        urgent = decision.outcome == Outcome.ESCALATE_URGENT
        lines = [
            ("URGENT - call back within 15 minutes" if urgent else "Call back requested") + " (Riya, Vaani)",
            f"Name: {_text(entities.get('caller_name')) or '-'}",
            f"Phone: {caller or 'not captured'}",
            f"Outcome (engine): {decision.outcome.value}; Riya said: {tag or '-'}",
            "CHECK: Riya and the qualification engine disagree." if "AGENT_MISMATCH" in flags else None,
            f"Summary: {_text(call.get('call_summary')) or '-'}",
            f"Call: {record.transcript_url}",
        ]
        try:
            text = "\n".join(line for line in lines if line)
            notify.to_studio_head(text, prefix="URGENT: " if urgent else "CALL BACK: ")
        except Exception:
            log.exception("call-back notice failed for %s", record.platform_call_id)
    return record


def sync(crm: CRM, pages: int = 2) -> dict:
    s = get_settings()
    if not s.vaani_api_key:
        return {"enabled": False}
    imported, skipped, errors = 0, 0, 0
    today = clock.now().date()
    for page in range(1, pages + 1):
        data = get_api().call_history(page=page)
        for call in data.get("data") or []:
            if s.vaani_agent_id and str(call.get("agent_id")) != s.vaani_agent_id:
                continue
            existing = crm.get_call_by_platform_id(str(call.get("call_id")))
            if (existing and existing.logged_by_agent) or not _ready(call):
                skipped += 1
                continue
            try:
                import_call(crm, call, today)
                imported += 1
            except Exception:
                errors += 1
                log.exception("vaani import failed for %s", call.get("call_id"))
        if not (data.get("pagination") or {}).get("has_next"):
            break
    return {"enabled": True, "imported": imported, "skipped": skipped, "errors": errors}


_last_sync = 0.0


def maybe_sync(crm: CRM, every_seconds: float = 60) -> None:
    """Called when the dashboard loads: at most once a minute per server, never raises."""
    global _last_sync
    if not get_settings().vaani_api_key or time.monotonic() - _last_sync < every_seconds:
        return
    _last_sync = time.monotonic()
    try:
        sync(crm, pages=1)
    except Exception:
        log.exception("vaani sync failed")

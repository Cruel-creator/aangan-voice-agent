import json
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from tests.conftest import IST, NOW, SECRET

FIXTURES = Path(__file__).parent / "fixtures" / "platform"
CALLER = "+919822012345"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def envelope(args: dict, call_id: str = "call-uuid-1") -> dict:
    """Platform-neutral tool request (docs/PLATFORM_API.md)."""
    return {"call_id": call_id, "caller_number": CALLER, "arguments": args}


def call_tool(w, name, args, call_id="call-uuid-1"):
    resp = w.client.post(f"/tools/{name}", json=envelope(args, call_id), headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


LEAD = dict(
    intent="new",
    name="Priya",
    locality="Wakad",
    property_type="apartment",
    project_category="residential",
    rooms=["kitchen", "living", "bedroom", "bedroom"],
    area_sqft=950,
    service="design_and_execution",
    completion_date="November",
    flexible=False,
    decision_maker="owner",
)


# ---------- auth + contract ----------
@pytest.mark.parametrize("path", ["/tools/check_scope", "/webhooks/end-of-call"])
def test_unauthenticated_requests_get_401(world, path):
    assert world.client.post(path, json={}).status_code == 401
    assert world.client.post(path, json={}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_x_webhook_secret_header_accepted(world):
    resp = world.client.post(
        "/tools/check_service_area",
        json=envelope({"locality": "Baner"}),
        headers={"X-Webhook-Secret": SECRET},
    )
    assert resp.json() == {"result": "in"}


def test_missing_secret_config_fails_closed(world, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("TOOL_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    assert world.client.post("/tools/check_scope", json=envelope({}), headers=AUTH).status_code == 503


def test_telegram_and_cron_require_auth_but_website_is_public(world):
    assert world.client.post("/webhooks/telegram", json={}).status_code == 401
    assert world.client.post("/cron/reroute-handoffs").status_code == 401
    # The website and dashboard are public by the owner's choice.
    for page in ("/", "/dashboard", "/dashboard/api/metrics", "/dashboard/api/calls", "/demo/status"):
        assert world.client.get(page).status_code == 200, page


def test_unknown_tool_404_and_bad_envelope_422(world):
    assert world.client.post("/tools/nope", json=envelope({}), headers=AUTH).status_code == 404
    assert world.client.post("/tools/check_scope", json={"arguments": {}}, headers=AUTH).status_code == 422
    extra = {**envelope({}), "surprise": 1}
    assert world.client.post("/tools/check_scope", json=extra, headers=AUTH).status_code == 422


def test_invalid_arguments_return_422_with_field_names(world):
    resp = world.client.post("/tools/get_slots", json=envelope({"consult_type": "zoom"}), headers=AUTH)
    assert resp.status_code == 422 and resp.json()["fields"] == ["consult_type"]


# ---------- qualification tools ----------
def test_check_tools(world):
    assert (
        call_tool(
            world, "check_scope", {"scope_description": "restaurant interiors", "property_type": "restaurant"}
        )["result"]
        == "fail"
    )
    assert call_tool(world, "check_service_area", {"locality": "Pimple Saudaagar"}) == {"result": "in"}
    assert call_tool(world, "check_timeline", {"completion_date": "November", "flexible": False}) == {
        "result": "tight"
    }
    assert call_tool(
        world,
        "check_budget",
        {
            "rooms": ["kitchen", "bedroom"],
            "property_type": "apartment",
            "area_sqft": 550,
            "budget_inr": 100000,
        },
    ) == {"result": "clearly_below"}


# ---------- lookup ----------
def test_lookup_caller_new_then_prior_enquiry(world):
    out = call_tool(world, "lookup_caller", {})
    assert out["existing_client"] is False and out["prior_enquiry"] is None
    assert out["office_hours_now"] is True
    call_tool(world, "log_call", LEAD)
    world.advance(days=2)
    out = call_tool(world, "lookup_caller", {}, call_id="call-uuid-2")
    assert out["prior_enquiry"]["locality"] == "Wakad"
    assert out["recent_dropped_call"] is False


# ---------- slots + booking ----------
def test_get_slots_rules(world):
    out = call_tool(
        world, "get_slots", {"consult_type": "studio", "locality": "Kharadi", "project_type": "residential"}
    )
    slots = out["slots"]
    assert 2 <= len(slots) <= 3
    starts = [datetime.fromisoformat(s["start"]) for s in slots]
    assert len({s.date() for s in starts}) == len(starts)  # different days
    for s, raw in zip(starts, slots, strict=True):
        assert s.weekday() < 5  # studio consults on working days only
        assert time(10) <= s.time() and (s + timedelta(minutes=60)).time() <= time(19)
        assert s - NOW >= timedelta(hours=18)
        assert raw["designer_id"] == "d-east"  # only East covers Kharadi


def test_get_slots_commercial_only_from_commercial_designers(world):
    out = call_tool(
        world, "get_slots", {"consult_type": "studio", "locality": "Baner", "project_type": "commercial"}
    )
    assert {s["designer_id"] for s in out["slots"]} == {"d-west"}


def test_site_visits_can_use_weekends(world):
    out = call_tool(world, "get_slots", {"consult_type": "site", "locality": "Kharadi"})
    first = datetime.fromisoformat(out["slots"][0]["start"])
    assert first.weekday() == 4  # Friday is next day; weekend allowed but not skipped to
    out_days = [datetime.fromisoformat(s["start"]).weekday() for s in out["slots"]]
    assert 5 in out_days  # Saturday offered for a site visit


def test_round_robin_prefers_least_recently_assigned(world):
    world.crm.touch_designer("d-east", NOW - timedelta(days=1))
    out = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Baner"})
    assert out["slots"][0]["designer_id"] == "d-west"


def test_booking_confirms_only_after_calendar_success_and_sends_sms(world):
    slot = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})["slots"][0]
    out = call_tool(
        world, "book_consultation", {**LEAD, "slot": slot["start"], "designer_id": slot["designer_id"]}
    )
    assert out["confirmed"] is True and out["booking_id"]
    assert len(world.cal.events["west@cal"]) == 1
    assert world.sms.sent and world.sms.sent[0][0] == CALLER
    lead = world.crm.leads_for_calls([world.crm.get_call_by_platform_id("call-uuid-1").id])[0]
    assert lead.status == "booked" and lead.designer_id == "d-west"


def test_failed_insert_retries_once_then_returns_confirmed_false(world):
    slot = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})["slots"][0]
    world.cal.fail_inserts = 1
    ok = call_tool(
        world, "book_consultation", {**LEAD, "slot": slot["start"], "designer_id": slot["designer_id"]}
    )
    assert ok["confirmed"] is True  # one retry succeeded

    slot2 = call_tool(
        world, "get_slots", {"consult_type": "studio", "locality": "Wakad"}, call_id="call-uuid-9"
    )["slots"][0]
    world.cal.fail_inserts = 2
    out = call_tool(
        world,
        "book_consultation",
        {**LEAD, "slot": slot2["start"], "designer_id": slot2["designer_id"]},
        call_id="call-uuid-9",
    )
    assert out == {"confirmed": False, "booking_id": None, "label": None, "reason": "calendar_insert_failed"}
    assert world.sms.sent[-1][1].count("confirmed") == 1 and len(world.sms.sent) == 1


def test_slot_taken_between_offer_and_booking(world):
    slot = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})["slots"][0]
    start = datetime.fromisoformat(slot["start"])
    world.cal.add("west@cal", start, start + timedelta(minutes=60))
    out = call_tool(
        world, "book_consultation", {**LEAD, "slot": slot["start"], "designer_id": slot["designer_id"]}
    )
    assert out["confirmed"] is False and out["reason"] == "slot_taken"


def test_concurrent_booking_race_backs_out_later_event(world):
    slot = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})["slots"][0]
    start = datetime.fromisoformat(slot["start"])

    def rival(cal, s, e):  # another server instance inserted first
        world.cal.on_insert = None
        world.cal.add(cal, s, e, created="2026-09-03T05:30:00.100Z")

    world.cal.on_insert = rival
    out = call_tool(
        world, "book_consultation", {**LEAD, "slot": slot["start"], "designer_id": slot["designer_id"]}
    )
    assert out["confirmed"] is False and out["reason"] == "slot_taken"
    assert len(world.cal.overlapping("west@cal", start, start + timedelta(minutes=60))) == 1


def test_booking_unknown_designer_or_past_slot(world):
    out = call_tool(
        world, "book_consultation", {**LEAD, "slot": "2026-09-10T11:00:00+05:30", "designer_id": "nope"}
    )
    assert out["confirmed"] is False
    out = call_tool(
        world, "book_consultation", {**LEAD, "slot": "2026-09-01T11:00:00+05:30", "designer_id": "d-west"}
    )
    assert out["confirmed"] is False


# ---------- handoff, escalation, review, logging ----------
def test_send_handoff_goes_to_designer_private_chat_with_flags(world):
    slot = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})["slots"][0]
    call_tool(world, "book_consultation", {**LEAD, "slot": slot["start"], "designer_id": slot["designer_id"]})
    assert call_tool(world, "send_handoff", LEAD) == {"sent": True}
    msg = world.tg.sent[-1]
    assert msg["chat_id"] == "101"
    assert "TIGHT" in msg["text"] and CALLER in msg["text"]
    buttons = [b["text"] for b in msg["reply_markup"]["inline_keyboard"][0]]
    assert buttons == ["Accept", "Calling now", "Reassign"]
    assert "/dashboard/calls/" in msg["text"]


def test_escalate_urgent_goes_to_studio_head_with_prefix(world):
    out = call_tool(world, "escalate_urgent", {"name": "Sheetal", "summary": "No reply in 5 days"})
    assert out == {"escalated": True}
    msg = world.tg.sent[-1]
    assert msg["chat_id"] == "999" and msg["text"].startswith("URGENT")
    call = world.crm.get_call_by_platform_id("call-uuid-1")
    assert call.outcome == "ESCALATE_URGENT"


def test_route_human_review(world):
    out = call_tool(
        world, "route_human_review", {**LEAD, "locality": "Alibaug", "open_question": "Is Alibaug served?"}
    )
    assert out == {"logged": True}
    assert world.crm.get_call_by_platform_id("call-uuid-1").outcome == "HUMAN_REVIEW"
    assert world.tg.sent[-1]["chat_id"] == "999"


def test_log_call_outcome_is_decided_in_code(world):
    call_tool(world, "log_call", {**LEAD, "completion_date": "in 3 weeks"})
    call = world.crm.get_call_by_platform_id("call-uuid-1")
    assert call.outcome == "DECLINE_TIMELINE" and call.logged_by_agent
    lead = world.crm.leads_for_calls([call.id])[0]
    assert lead.status == "re_engage"


# ---------- end of call ----------
def eoc():
    return json.loads((FIXTURES / "end_of_call_report.json").read_text(encoding="utf-8"))


def test_end_of_call_logs_even_without_log_call(world):
    resp = world.client.post("/webhooks/end-of-call", json=eoc(), headers=AUTH)
    assert resp.status_code == 200
    call = world.crm.get_call_by_platform_id("call-uuid-1")
    assert call.duration_seconds == 360 and call.price_leak is False
    assert call.cost_inr == round(0.42 * 85 + 6 * 1, 2)
    assert "PRICING_ASKED" in call.flags
    assert call.transcript_url.endswith(f"/dashboard/calls/{call.id}")
    assert call.phone_hash and CALLER not in (call.phone_encrypted or "")


def test_end_of_call_alerts_front_desk_when_qualified_without_handoff(world):
    call_tool(world, "log_call", {**LEAD, "completion_date": "March"})
    world.client.post("/webhooks/end-of-call", json=eoc(), headers=AUTH)
    alert = world.tg.sent[-1]
    assert alert["chat_id"] == "-100555" and "NO designer handoff" in alert["text"]
    assert CALLER not in alert["text"]  # front desk never gets the number


def test_end_of_call_no_alert_when_handed_off(world):
    call_tool(world, "send_handoff", {**LEAD, "completion_date": "March"})
    call_tool(world, "log_call", {**LEAD, "completion_date": "March"})
    n = len(world.tg.sent)
    world.client.post("/webhooks/end-of-call", json=eoc(), headers=AUTH)
    assert len(world.tg.sent) == n


def test_end_of_call_flags_spoken_price(world):
    body = eoc()
    body["messages"].append({"role": "agent", "text": "Usually it is around 8 lakh for a 2BHK."})
    world.client.post("/webhooks/end-of-call", json=body, headers=AUTH)
    call = world.crm.get_call_by_platform_id("call-uuid-1")
    assert call.price_leak is True and "PRICE_LEAK" in call.flags


def test_hindi_price_question_sets_pricing_asked(world):
    body = eoc()
    body["messages"] = [{"role": "caller", "text": "2BHK ka interior kitna padega?"}]
    world.client.post("/webhooks/end-of-call", json=body, headers=AUTH)
    assert "PRICING_ASKED" in world.crm.get_call_by_platform_id("call-uuid-1").flags


def test_hindi_spoken_price_is_flagged(world):
    body = eoc()
    body["messages"] = [{"role": "agent", "text": "लगभग पाँच लाख लगेंगे"}]
    world.client.post("/webhooks/end-of-call", json=body, headers=AUTH)
    assert world.crm.get_call_by_platform_id("call-uuid-1").price_leak is True


def test_end_of_call_transfer_and_pickup_recorded(world):
    body = {**eoc(), "transferred": True, "pickup_seconds": 22}
    body.pop("ended_reason")
    world.client.post("/webhooks/end-of-call", json=body, headers=AUTH)
    call = world.crm.get_call_by_platform_id("call-uuid-1")
    assert call.ended_reason == "transferred" and call.pickup_seconds == 22


def test_end_of_call_requires_call_id(world):
    resp = world.client.post("/webhooks/end-of-call", json={"messages": []}, headers=AUTH)
    assert resp.status_code == 422


# ---------- telegram buttons + jobs ----------
def _handoff(world):
    call_tool(world, "send_handoff", {**LEAD, "completion_date": "March"})
    return world.crm.db.tables["handoffs"][-1]["id"]


def tg_cb(world, data, user="aryan"):
    return world.client.post(
        "/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "data": data,
                "from": {"id": 7, "username": user},
                "message": {"message_id": 5, "chat": {"id": 101}},
            },
        },
    )


def test_accept_button_writes_ack(world):
    hid = _handoff(world)
    assert tg_cb(world, f"h:{hid}:a").json()["status"] == "accept"
    h = world.crm.get_handoff(hid)
    assert h.action == "accept" and h.acked_by == "aryan" and h.acked_at == NOW


def test_reroute_job_with_shortened_timer(world, monkeypatch):
    from app.config import get_rules

    monkeypatch.setattr(get_rules().routing, "handoff_ack_reroute_hours", 0.01)  # 36 s
    hid = _handoff(world)
    cron = {"X-Cron-Secret": "cron-secret"}
    assert world.client.post("/cron/reroute-handoffs", headers=cron).json() == {"rerouted": 0}
    world.advance(seconds=40)
    assert world.client.post("/cron/reroute-handoffs", headers=cron).json() == {"rerouted": 1}
    assert world.tg.sent[-1]["chat_id"] == "999"
    assert world.tg.sent[-1]["text"].startswith("RE-ROUTED")
    assert world.crm.get_handoff(hid).rerouted_at is not None
    assert world.client.post("/cron/reroute-handoffs", headers=cron).json() == {"rerouted": 0}


def test_acked_handoff_is_not_rerouted(world):
    hid = _handoff(world)
    tg_cb(world, f"h:{hid}:c")
    world.advance(hours=3)
    assert world.client.post("/cron/reroute-handoffs", headers={"X-Cron-Secret": "cron-secret"}).json() == {
        "rerouted": 0
    }


def test_consult_followup_buttons_write_outcomes(world):
    slot = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})["slots"][0]
    call_tool(world, "book_consultation", {**LEAD, "slot": slot["start"], "designer_id": slot["designer_id"]})
    cron = {"X-Cron-Secret": "cron-secret"}
    assert world.client.post("/cron/consult-followups", headers=cron).json() == {"sent": 0}
    world.state["now"] = datetime.fromisoformat(slot["start"]).astimezone(IST) + timedelta(hours=2)
    assert world.client.post("/cron/consult-followups", headers=cron).json() == {"sent": 1}
    assert world.client.post("/cron/consult-followups", headers=cron).json() == {"sent": 0}
    lead_id = world.crm.db.tables["leads"][0]["id"]
    tg_cb(world, f"o:{lead_id}:h")
    tg_cb(world, f"o:{lead_id}:w")
    outcome = world.crm.outcomes_for_leads([lead_id])[0]
    assert outcome.held is True and outcome.won is True

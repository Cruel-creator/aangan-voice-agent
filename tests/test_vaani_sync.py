"""Importing Vaani calls into the dashboard (fake Vaani API, shaped like its docs)."""

import json

import httpx
import pytest

from app.config import get_settings
from app.platform import vaani_sync

AGENT = "09b7036a-8759-41c7-8932-b0e5fb22e46f"
CALLER = "+919822012345"


def vaani_call(call_id="inbound-1", tag="CONSULT_REQUESTED", **entity):
    entities = {
        "caller_name": "Rohan",
        "intent": "new_project",
        "locality": "Baner",
        "property_type": "flat",
        "project_category": "residential",
        "rooms": "full home",
        "carpet_area_sqft": "1200",
        "completion_date": "March 2027",
        "service_wanted": "design_and_execution",
        "decision_maker": "owner",
        "budget_volunteered": "NA",
        "consult_type": "site",
        "preferred_day_time": "Saturday morning",
        "friend_of_nikhil": "no",
        "pricing_asked": "yes",
        **entity,
    }
    return {
        "call_id": call_id,
        "direction": "inbound",
        "agent_id": AGENT,
        "from_number": CALLER,
        "to_number": "+912000000000",
        "Start_time": "2026-09-03T05:00:00Z",
        "End_time": "2026-09-03T05:04:00Z",
        "duration_ms": 240000,
        "call_cost": 19.68,
        "call_status": "completed",
        "post_processing_status": "completed",
        "call_summary": "New 3BHK in Baner, wants a site visit.",
        "call_entity": entities,
        "call_eval_tag": {"call_outcome": tag},
        "call_transcription": "AGENT: Hello, thank you for calling Aangan Studio.\n"
        "USER: How much per square foot?\nAGENT: Pricing depends on the site.",
    }


class FakeVaani(vaani_sync.VaaniApi):
    def __init__(self, calls):
        self.calls, self.requests = calls, 0

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-api-key"] == "vaani_test"
            self.requests += 1
            return httpx.Response(200, json={"data": self.calls, "pagination": {"has_next": False}})

        super().__init__("vaani_test", transport=httpx.MockTransport(handler))


@pytest.fixture
def vaani(world, monkeypatch):
    monkeypatch.setenv("VAANI_API_KEY", "vaani_test")
    monkeypatch.setenv("VAANI_AGENT_ID", AGENT)
    get_settings.cache_clear()
    monkeypatch.setattr(vaani_sync, "_last_sync", 0.0)
    fake = FakeVaani([vaani_call()])
    vaani_sync.set_api(fake)
    yield fake
    vaani_sync.set_api(None)


def test_finished_call_is_logged_decided_in_code_and_called_back(world, vaani):
    assert vaani_sync.sync(world.crm) == {"enabled": True, "imported": 1, "skipped": 0, "errors": 0}
    call = world.crm.get_call_by_platform_id("inbound-1")
    assert call.outcome == "QUALIFIED" and call.criteria["agent_outcome"] == "CONSULT_REQUESTED"
    assert {"VAANI", "PRICING_ASKED"} <= set(call.flags) and "AGENT_MISMATCH" not in call.flags
    assert call.duration_seconds == 240 and call.cost_inr is not None and call.summary
    lead = world.crm.leads_for_calls([call.id])[0]
    assert (lead.name, lead.locality, lead.consult_type) == ("Rohan", "Baner", "site")
    assert "Saturday morning" in lead.open_question
    notice = world.tg.sent[-1]
    assert notice["chat_id"] == "999" and CALLER in notice["text"] and "Call back requested" in notice["text"]
    # Already imported: the next sync leaves it alone and sends nothing new.
    sent = len(world.tg.sent)
    assert vaani_sync.sync(world.crm)["skipped"] == 1 and len(world.tg.sent) == sent


def test_public_dashboard_shows_the_call_but_never_the_number(world, vaani):
    calls = world.client.get("/dashboard/api/calls").json()["calls"]
    assert [c["outcome"] for c in calls] == ["QUALIFIED"]
    detail = world.client.get(f"/dashboard/api/calls/{calls[0]['id']}").json()
    assert CALLER not in json.dumps(detail) and detail["transcript"][0]["speaker"] == "agent"


def test_engine_overrules_agent_on_budget_and_flags_it(world, vaani):
    vaani.calls = [vaani_call(budget_volunteered="1 lakh")]
    vaani_sync.sync(world.crm)
    call = world.crm.get_call_by_platform_id("inbound-1")
    assert call.outcome == "BUDGET_MISALIGNED" and "AGENT_MISMATCH" in call.flags
    # Riya promised a call back, so the team still gets one, marked for checking.
    assert "disagree" in world.tg.sent[-1]["text"]


def test_complaint_is_urgent_and_other_agents_or_unfinished_calls_are_ignored(world, vaani):
    unfinished = {**vaani_call("inbound-3"), "End_time": None}
    other_agent = {**vaani_call("inbound-4"), "agent_id": "someone-else"}
    complaint = vaani_call("inbound-2", tag="URGENT_COMPLAINT", intent="existing_client_complaint")
    vaani.calls = [complaint, unfinished, other_agent]
    assert vaani_sync.sync(world.crm)["imported"] == 1
    assert world.crm.get_call_by_platform_id("inbound-2").outcome == "ESCALATE_URGENT"
    assert world.crm.get_call_by_platform_id("inbound-3") is None
    assert world.crm.get_call_by_platform_id("inbound-4") is None
    assert world.tg.sent[-1]["text"].startswith("URGENT")


def test_spoken_price_is_flagged(world, vaani):
    leaky = vaani_call()
    leaky["call_transcription"] += "\nAGENT: It's about 8 lakh for your flat."
    vaani.calls = [leaky]
    vaani_sync.sync(world.crm)
    assert "PRICE_LEAK" in world.crm.get_call_by_platform_id("inbound-1").flags


def test_entities_as_list_or_json_and_budget_words():
    as_list = [{"name": "locality", "value": "Wakad"}, {"name": "rooms", "value": "kitchen and living room"}]
    assert vaani_sync._as_dict(as_list) == {"locality": "Wakad", "rooms": "kitchen and living room"}
    assert vaani_sync._as_dict(json.dumps({"locality": "Aundh"})) == {"locality": "Aundh"}
    facts = vaani_sync.facts_from(vaani_sync._as_dict(as_list), "CONSULT_REQUESTED")
    assert facts.rooms == ["kitchen", "living room"] and facts.intent == "new"
    assert vaani_sync.budget_inr("around 20 lakh") == 2_000_000
    assert vaani_sync.budget_inr("1.5 crore") == 15_000_000
    assert vaani_sync.budget_inr("50k") == 50_000
    assert vaani_sync.budget_inr("NA") is None


def test_dashboard_sync_is_throttled_and_off_without_key(world, vaani, monkeypatch):
    world.client.get("/dashboard/api/metrics")
    world.client.get("/dashboard/api/calls")
    assert vaani.requests == 1  # second load within a minute doesn't call Vaani again
    monkeypatch.delenv("VAANI_API_KEY")
    get_settings.cache_clear()
    assert vaani_sync.sync(world.crm) == {"enabled": False}


def test_cron_endpoint(world, vaani):
    resp = world.client.post("/cron/vaani-sync", headers={"X-Cron-Secret": "cron-secret"})
    assert resp.json()["imported"] == 1
    assert world.client.post("/cron/vaani-sync").status_code == 401

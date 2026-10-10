"""Website receptionist demo, with a scripted stand-in for Claude (no API calls, no cost)."""

import json
from dataclasses import dataclass, field

import pytest

from app import demo
from app.config import get_settings
from tests.test_api import LEAD
from tests.test_price_leak import assert_no_price


@dataclass
class Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None

    def model_dump(self, mode="json", exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Response:
    content: list
    stop_reason: str


def text(t):
    return Response([Block("text", text=t)], "end_turn")


def tool(name, args, n=[0]):  # noqa: B006 - counter for unique ids
    n[0] += 1
    return Response([Block("tool_use", id=f"tu_{n[0]}", name=name, input=args)], "tool_use")


@dataclass
class FakeClient:
    script: list
    calls: list = field(default_factory=list)

    @property
    def beta(self):
        return self

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw["messages"])))  # snapshot what was sent
        step = self.script.pop(0)
        return step(kw["messages"]) if callable(step) else step


def last_tool_result(messages):
    return json.loads(messages[-1]["content"][-1]["content"])


@pytest.fixture
def live(world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    yield world
    demo.set_client(None)


def chat(w, message, session_id=None, language="en"):
    return w.client.post(
        "/demo/chat", json={"session_id": session_id, "message": message, "language": language}
    )


def test_disabled_without_api_key(world):
    assert world.client.get("/demo/status").json()["enabled"] is False
    resp = chat(world, "hello")
    assert resp.status_code == 503 and "isn't switched on" in resp.json()["error"]


def test_status_when_enabled(live):
    s = live.client.get("/demo/status").json()
    assert s["enabled"] is True and s["greeting"].startswith("Hello, thank you for calling Aangan Studio")


def test_conversation_runs_real_tools_and_is_stored_append_only(live):
    fake = FakeClient(
        [
            tool("check_service_area", {"locality": "Wakad"}),
            text("Wakad is in our area. When do you need it done?"),
            text("Got it."),
        ]
    )
    demo.set_client(fake)
    first = chat(live, "I have a 2BHK in Wakad").json()
    assert first["reply"] == "Wakad is in our area. When do you need it done?"
    assert last_tool_result(fake.calls[1]) == {"result": "in"}  # the real tool ran

    second = chat(live, "By March", session_id=first["session_id"]).json()
    assert second["session_id"] == first["session_id"] and second["turns_left"] == 28
    # Turn 2 resent turn 1 exactly (append-only history, tool results included).
    assert fake.calls[2][: len(fake.calls[1]) + 1] == fake.calls[1] + [fake.calls[2][len(fake.calls[1])]]

    call = live.crm.get_call(first["call_id"])
    assert call.is_demo and call.transcript.splitlines() == [
        "Visitor: I have a 2BHK in Wakad",
        "AI: Wakad is in our area. When do you need it done?",
        "Visitor: By March",
        "AI: Got it.",
    ]


def test_demo_booking_touches_no_real_calendar_telegram_or_sms(live):
    def book(messages):
        slot = last_tool_result(messages)["slots"][0]
        return tool("book_consultation", {**LEAD, "slot": slot["start"], "designer_id": slot["designer_id"]})

    fake = FakeClient(
        [
            tool("get_slots", {"consult_type": "studio", "locality": "Wakad"}),
            book,
            tool("send_handoff", LEAD),
            tool("escalate_urgent", {"summary": "testing"}),
            text("You're booked for Friday 4 September at 10:00 AM. This was a demo booking."),
        ]
    )
    demo.set_client(fake)
    out = chat(live, "Book me in").json()
    booked = json.loads(fake.calls[2][-1]["content"][-1]["content"])
    assert booked["confirmed"] is True and booked["booking_id"].startswith("demo-")

    assert live.cal.events == {} and live.tg.sent == [] and live.sms.sent == []
    assert live.crm.db.tables.get("handoffs", []) == []
    lead = live.crm.leads_for_calls([out["call_id"]])[0]
    assert lead.status == "booked" and lead.designer_id is None  # follow-up job skips it
    assert live.crm.get_call(out["call_id"]).is_demo


def test_tool_errors_go_back_to_the_model_not_the_visitor(live):
    fake = FakeClient(
        [
            tool("get_slots", {"consult_type": "zoom"}),
            tool("no_such_tool", {}),
            text("Let me try that differently."),
        ]
    )
    demo.set_client(fake)
    assert chat(live, "slots please").json()["reply"] == "Let me try that differently."
    bad_args = fake.calls[1][-1]["content"][0]
    assert bad_args["is_error"] is True and json.loads(bad_args["content"])["fields"] == ["consult_type"]
    assert fake.calls[2][-1]["content"][0]["is_error"] is True


def test_session_and_daily_limits(live, monkeypatch):
    monkeypatch.setenv("DEMO_SESSION_TURN_LIMIT", "1")
    get_settings.cache_clear()
    demo.set_client(FakeClient([text("Hi."), text("Hi again.")]))
    first = chat(live, "hello").json()
    resp = chat(live, "again", session_id=first["session_id"])
    assert resp.status_code == 429 and "length limit" in resp.json()["error"]

    monkeypatch.setenv("DEMO_DAILY_TURN_LIMIT", "1")
    get_settings.cache_clear()
    resp = chat(live, "new conversation")
    assert resp.status_code == 429 and "today's usage limit" in resp.json()["error"]


def test_spoken_price_is_flagged_and_pricing_question_noted(live):
    demo.set_client(FakeClient([text("It's around 8 lakh for a 2BHK.")]))
    out = chat(live, "How much does it cost per sq ft?").json()
    assert {"PRICE_LEAK", "PRICING_ASKED"} <= set(out["flags"])
    assert live.crm.get_call(out["call_id"]).price_leak is True


def test_refusal_and_input_validation(live):
    demo.set_client(FakeClient([Response([], "refusal")]))
    assert chat(live, "something off-topic").json()["reply"] == demo.REFUSAL_REPLY
    assert chat(live, "   ").status_code == 422
    assert chat(live, "x" * 1001).status_code == 422
    # A made-up session id starts a fresh conversation instead of erroring.
    demo.set_client(FakeClient([text("Hello.")]))
    assert chat(live, "hi", session_id="not-a-uuid").status_code == 200


def test_demo_instructions_contain_no_price():
    assert_no_price(demo.DEMO_NOTE, "demo instructions")
    for block in demo._system("hi", __import__("datetime").datetime(2026, 9, 3, 11)):
        assert_no_price(block["text"], "demo system prompt")


def test_redact_keeps_dates_and_areas():
    assert (
        demo.redact("call 9822012345, 1,400 sq ft, on 2026-09-10")
        == "call [number hidden], 1,400 sq ft, on 2026-09-10"
    )

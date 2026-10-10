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
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "DEMO_PROVIDER", "DEMO_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    yield world
    demo.set_client(None)
    demo.set_client(None, "gemini")


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


# ---------- Gemini (scripted with the SDK's own types, so serialisation is real) ----------
from google.genai import types as gt  # noqa: E402


def g_resp(*parts, finish="STOP"):
    content = gt.Content(role="model", parts=list(parts)) if parts else None
    return gt.GenerateContentResponse(candidates=[gt.Candidate(content=content, finish_reason=finish)])


def g_text(t, signature=None):
    return g_resp(gt.Part(text=t, thought_signature=signature))


def g_call(name, args, n=[0]):  # noqa: B006 - counter for unique ids
    n[0] += 1
    call = gt.FunctionCall(id=f"fc_{n[0]}", name=name, args=args)
    return g_resp(gt.Part(function_call=call, thought_signature=f"sig-{n[0]}".encode()))


@dataclass
class FakeGemini:
    script: list
    calls: list = field(default_factory=list)
    configs: list = field(default_factory=list)

    @property
    def models(self):
        return self

    def generate_content(self, *, model, contents, config):
        sent = [c.model_dump(mode="json", exclude_none=True) for c in contents]
        self.calls.append(sent)
        self.configs.append(config)
        step = self.script.pop(0)
        return step(sent) if callable(step) else step


def last_function_response(contents):
    return contents[-1]["parts"][-1]["function_response"]


@pytest.fixture
def gemini(live, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    get_settings.cache_clear()
    return live


def test_gemini_preferred_when_both_keys_set(gemini, monkeypatch):
    assert demo.provider() == "gemini" and gemini.client.get("/demo/status").json()["enabled"]
    monkeypatch.setenv("DEMO_PROVIDER", "claude")
    get_settings.cache_clear()
    assert demo.provider() == "claude"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    get_settings.cache_clear()
    assert demo.provider() is None  # forced to a provider with no key: demo stays off


def test_gemini_conversation_runs_real_tools_and_keeps_thought_signatures(gemini):
    first_call = g_call("check_service_area", {"locality": "Wakad"})
    fake = FakeGemini(
        [
            first_call,
            g_text("Wakad is in our area. When do you need it done?", b"sig-text"),
            g_text("Got it."),
        ]
    )
    demo.set_client(fake, "gemini")
    first = chat(gemini, "I have a 2BHK in Wakad").json()
    assert first["reply"] == "Wakad is in our area. When do you need it done?"

    fn_call = first_call.candidates[0].content.parts[0].function_call
    response = last_function_response(fake.calls[1])
    assert response == {"id": fn_call.id, "name": "check_service_area", "response": {"result": "in"}}
    # The model's turn went back byte-for-byte, signature included.
    sent_back = gt.Content.model_validate(fake.calls[1][-2])
    assert sent_back.parts[0].thought_signature == first_call.candidates[0].content.parts[0].thought_signature

    second = chat(gemini, "By March", session_id=first["session_id"]).json()
    assert second["session_id"] == first["session_id"]
    assert fake.calls[2][: len(fake.calls[1])] == fake.calls[1]  # append-only history
    assert gt.Content.model_validate(fake.calls[2][-2]).parts[0].thought_signature == b"sig-text"
    assert fake.calls[2][-1] == {"role": "user", "parts": [{"text": "By March"}]}

    config = fake.configs[0]
    assert config.automatic_function_calling.disable is True
    assert len(config.tools[0].function_declarations) == len(demo.TOOLS)
    assert demo.DEMO_NOTE.strip() in config.system_instruction
    assert_no_price(config.system_instruction, "gemini system instruction")
    assert gemini.crm.get_call(first["call_id"]).transcript.splitlines()[-1] == "AI: Got it."


def test_gemini_tool_errors_go_back_to_the_model(gemini):
    fake = FakeGemini([g_call("get_slots", {"consult_type": "zoom"}), g_text("Let me try that differently.")])
    demo.set_client(fake, "gemini")
    assert chat(gemini, "slots please").json()["reply"] == "Let me try that differently."
    assert last_function_response(fake.calls[1])["response"]["fields"] == ["consult_type"]


def test_gemini_block_is_a_refusal_and_is_not_kept_in_history(gemini):
    fake = FakeGemini([g_resp(finish="SAFETY"), g_text("Hello again.")])
    demo.set_client(fake, "gemini")
    first = chat(gemini, "something off-topic").json()
    assert first["reply"] == demo.REFUSAL_REPLY
    assert chat(gemini, "hi", session_id=first["session_id"]).json()["reply"] == "Hello again."
    assert fake.calls[1] == [{"role": "user", "parts": [{"text": "hi"}]}]
    # The visitor-facing transcript still records both exchanges.
    assert len(gemini.crm.get_call(first["call_id"]).transcript.splitlines()) == 4


def test_switching_provider_starts_a_fresh_conversation(live, monkeypatch):
    demo.set_client(FakeClient([text("Hi from Claude.")]))
    first = chat(live, "hello").json()
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    get_settings.cache_clear()
    demo.set_client(FakeGemini([g_text("Hi from Gemini.")]), "gemini")
    second = chat(live, "hello again", session_id=first["session_id"]).json()
    assert second["reply"] == "Hi from Gemini." and second["session_id"] != first["session_id"]


def test_redact_keeps_dates_and_areas():
    assert (
        demo.redact("call 9822012345, 1,400 sq ft, on 2026-09-10")
        == "call [number hidden], 1,400 sq ft, on 2026-09-10"
    )

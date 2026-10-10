"""Website receptionist demo: a visitor talks or types to the AI receptionist in the browser.

Same system prompt, same tools and the same qualification rules as the phone agent. Tool
calls run in demo mode (CallContext.demo=True): decisions are made and recorded as usual,
but no real calendar, Telegram or SMS is touched, and the call is flagged is_demo so it
stays out of the real numbers unless the dashboard is asked to include it.

The conversation (including the model's thinking blocks) is stored per session in
Postgres and only ever appended to, as the Claude API requires for multi-turn tool use.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, time
from typing import Any

from pydantic import ValidationError

from app.config import PROMPT_PATH, get_settings
from app.platform.webhooks import CallContext
from app.services import clock
from app.services.crm import CallRecord, get_crm
from app.services.jobs import PRICING_ASK
from app.services.price_audit import find_price_mentions
from app.tools.handlers import TOOLS
from app.tools.schemas import anthropic_tools, greeting

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
MAX_MODEL_ROUNDS = 8  # model calls per visitor message (each tool round is one call)
LANGUAGES = {"en": "English", "hi": "Hindi"}
REFUSAL_REPLY = "Sorry, I can't help with that here. Is there an interior project I can help you with?"

DEMO_NOTE = f"""

WEBSITE DEMO MODE
You are talking to a visitor on Aangan Studio's website, through a browser chat or
microphone, not a phone line. The visitor has already heard your greeting:
"{greeting()}"
- Everything works as on a call and every rule above still applies, including pricing.
- Your replies are read aloud. Keep each reply to one to three short sentences, plain
  text only: no lists, no markdown, no emojis.
- There is no caller phone number. Do not promise an SMS; say a real call would also get
  an SMS confirmation.
- Bookings in this demo are not real appointments. When book_consultation returns
  confirmed: true, read back the slot and add that this was a demo booking.
- If the visitor asks for a person, explain that on a real call you would connect them to
  the studio, and offer to note their details.
"""


class DemoUnavailable(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message, self.status = message, status


_client: Any = None


def get_client() -> Any:
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key, max_retries=2)
    return _client


def set_client(client: Any) -> None:
    """Tests inject a fake Claude client here."""
    global _client
    _client = client


def _system(language: str, now: datetime) -> list[dict]:
    base = PROMPT_PATH.read_text(encoding="utf-8") + DEMO_NOTE
    dynamic = (
        f"Today is {now:%A %d %B %Y}, {now:%I:%M %p} India time. The visitor chose "
        f"{LANGUAGES.get(language, 'English')}; reply in that language (Hindi in Devanagari "
        "script), and follow them if they switch between English and Hindi."
    )
    # Stable prompt first with a cache breakpoint; the per-request line goes after it.
    return [
        {"type": "text", "text": base, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic},
    ]


def _run_tool(name: str, raw_input: dict, ctx: CallContext) -> tuple[str, bool]:
    """Returns (JSON result, is_error). Never raises into the conversation."""
    if name not in TOOLS:
        return json.dumps({"error": f"unknown tool {name}"}), True
    model, handler = TOOLS[name]
    try:
        args = model.model_validate(raw_input)
    except ValidationError as e:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
        return json.dumps({"error": "invalid arguments", "fields": fields}), True
    try:
        return json.dumps(handler(args, ctx).model_dump(mode="json")), False
    except Exception:
        log.exception("demo tool %s failed", name)
        return json.dumps({"error": "temporary problem, please continue"}), True


def _block_dicts(content) -> list[dict]:
    return [b.model_dump(mode="json", exclude_none=True) for b in content]


def _text(content) -> str:
    return " ".join(b.text for b in content if getattr(b, "type", "") == "text").strip()


def _start_of_today(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)


def status() -> dict:
    s = get_settings()
    return {
        "enabled": bool(s.anthropic_api_key),
        "greeting": greeting(),
        "languages": LANGUAGES,
        "session_turn_limit": s.demo_session_turn_limit,
    }


def chat(session_id: str | None, text: str, language: str) -> dict:
    s = get_settings()
    if not s.anthropic_api_key:
        raise DemoUnavailable("The live demo isn't switched on yet (no Claude API key configured).", 503)
    text = (text or "").strip()
    if not text:
        raise DemoUnavailable("Say or type something first.", 422)
    if len(text) > MAX_MESSAGE_CHARS:
        raise DemoUnavailable(f"Please keep messages under {MAX_MESSAGE_CHARS} characters.", 422)
    language = language if language in LANGUAGES else "en"

    crm, now = get_crm(), clock.now()
    if crm.demo_turns_since(_start_of_today(now)) >= s.demo_daily_turn_limit:
        raise DemoUnavailable("The demo has reached today's usage limit. Please try again tomorrow.", 429)

    session = crm.get_demo_session(session_id) if session_id else None
    if session is None:
        call = crm.upsert_call(
            CallRecord(
                platform_call_id=f"web-demo-{uuid.uuid4()}",
                is_demo=True,
                started_at=now,
                in_office_hours=clock.in_office_hours(now),
                ended_reason="web_demo",
            )
        )
        session = crm.create_demo_session(call.id, language, now)
    if session.turns >= s.demo_session_turn_limit:
        raise DemoUnavailable("This conversation has reached its length limit. Start a new one.", 429)
    call = crm.get_call(session.call_id)
    ctx = CallContext(call_id=call.platform_call_id, demo=True)

    messages = list(session.messages) + [{"role": "user", "content": text}]
    reply = ""
    for _ in range(MAX_MODEL_ROUNDS):
        resp = get_client().beta.messages.create(
            model=s.demo_model,
            max_tokens=4096,
            system=_system(language, now),
            tools=anthropic_tools(),
            messages=messages,
            output_config={"effort": s.demo_effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        messages.append({"role": "assistant", "content": _block_dicts(resp.content)})
        if resp.stop_reason == "refusal":
            reply = REFUSAL_REPLY
            break
        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            reply = _text(resp.content)
            break
        results = []
        for u in uses:
            content, is_error = _run_tool(u.name, u.input, ctx)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": u.id,
                    "content": content,
                    **({"is_error": True} if is_error else {}),
                }
            )
        messages.append({"role": "user", "content": results})
    else:
        reply = "Sorry, I'm having trouble right now. Could you say that again?"
    reply = reply or "Sorry, could you say that again?"

    session.messages, session.turns, session.language = messages, session.turns + 1, language
    crm.save_demo_session(session, clock.now())
    _update_call(call, text, reply, now)
    call = crm.get_call(call.id)
    return {
        "session_id": session.id,
        "reply": reply,
        "outcome": call.outcome,
        "flags": call.flags or [],
        "turns_left": max(s.demo_session_turn_limit - session.turns, 0),
        "call_id": call.id,
    }


def _update_call(call: CallRecord, visitor: str, reply: str, now: datetime) -> None:
    crm = get_crm()
    transcript = (
        (call.transcript or "") + ("\n" if call.transcript else "") + f"Visitor: {visitor}\nAI: {reply}"
    )
    flags = set(call.flags or [])
    if PRICING_ASK.search(visitor):
        flags.add("PRICING_ASKED")
    leak = bool(find_price_mentions([reply])) or bool(call.price_leak)
    if leak:
        flags.add("PRICE_LEAK")
    ended = clock.now()
    started = call.started_at or now
    crm.upsert_call(
        CallRecord(
            platform_call_id=call.platform_call_id,
            transcript=transcript,
            ended_at=ended,
            duration_seconds=max((ended - started).total_seconds(), 0.0),
            price_leak=leak,
            flags=sorted(flags),
            transcript_url=f"/dashboard/calls/{call.id}",
        )
    )


PHONE_LIKE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact(text: str | None) -> str:
    """Public pages never show phone numbers or email addresses typed or spoken in a call.
    A run counts as a phone number only with 10+ digits, so dates and areas stay readable."""

    def phone(m: re.Match) -> str:
        return "[number hidden]" if sum(ch.isdigit() for ch in m.group()) >= 10 else m.group()

    return EMAIL.sub("[email hidden]", PHONE_LIKE.sub(phone, text or ""))

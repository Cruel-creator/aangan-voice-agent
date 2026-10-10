"""Website receptionist demo: a visitor talks or types to the AI receptionist in the browser.

Same system prompt, same tools and the same qualification rules as the phone agent. Tool
calls run in demo mode (CallContext.demo=True): decisions are made and recorded as usual,
but no real calendar, Telegram or SMS is touched, and the call is flagged is_demo so it
stays out of the real numbers unless the dashboard is asked to include it.

The model is Gemini or Claude (Settings.demo_provider; "auto" prefers Gemini when its key is
set). The conversation, including the model's thinking blocks or thought signatures, is
stored per session in Postgres in that provider's own message format and only ever appended
to, as both APIs require for multi-turn tool use. A session started on one provider is not
continued on the other; the visitor just gets a fresh conversation.
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
from app.tools.schemas import anthropic_tools, gemini_tools, greeting

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
MAX_MODEL_ROUNDS = 8  # model calls per visitor message (each tool round is one call)
LANGUAGES = {"en": "English", "hi": "Hindi"}
REFUSAL_REPLY = "Sorry, I can't help with that here. Is there an interior project I can help you with?"
TROUBLE_REPLY = "Sorry, I'm having trouble right now. Could you say that again?"
DEFAULT_MODELS = {"gemini": "gemini-3.8-flash", "claude": "claude-opus-5-5"}
# Gemini stops for these when it won't answer; treat like a Claude refusal.
GEMINI_BLOCKED = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}

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


_clients: dict[str, Any] = {}


def provider() -> str | None:
    """Which model provider answers visitors: "gemini", "claude", or None (demo off)."""
    s = get_settings()
    keys = {"gemini": s.gemini_api_key, "claude": s.anthropic_api_key}
    if s.demo_provider != "auto":
        return s.demo_provider if keys[s.demo_provider] else None
    return next((p for p in ("gemini", "claude") if keys[p]), None)


def get_client(name: str = "claude") -> Any:
    if name not in _clients:
        s = get_settings()
        if name == "gemini":
            from google import genai

            _clients[name] = genai.Client(api_key=s.gemini_api_key)
        else:
            import anthropic

            _clients[name] = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=2)
    return _clients[name]


def set_client(client: Any, name: str = "claude") -> None:
    """Tests inject a fake client here (None clears it)."""
    if client is None:
        _clients.pop(name, None)
    else:
        _clients[name] = client


def _system_parts(language: str, now: datetime) -> tuple[str, str]:
    base = PROMPT_PATH.read_text(encoding="utf-8") + DEMO_NOTE
    dynamic = (
        f"Today is {now:%A %d %B %Y}, {now:%I:%M %p} India time. The visitor chose "
        f"{LANGUAGES.get(language, 'English')}; reply in that language (Hindi in Devanagari "
        "script), and follow them if they switch between English and Hindi."
    )
    return base, dynamic


def _system(language: str, now: datetime) -> list[dict]:
    base, dynamic = _system_parts(language, now)
    # Stable prompt first with a cache breakpoint; the per-request line goes after it.
    return [
        {"type": "text", "text": base, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic},
    ]


def _format_of(messages: list[dict]) -> str | None:
    """Which provider wrote a stored conversation (Gemini messages carry "parts")."""
    if not messages:
        return None
    return "gemini" if any("parts" in m for m in messages) else "claude"


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
        "enabled": provider() is not None,
        "greeting": greeting(),
        "languages": LANGUAGES,
        "session_turn_limit": s.demo_session_turn_limit,
    }


def chat(session_id: str | None, text: str, language: str) -> dict:
    s = get_settings()
    which = provider()
    if which is None:
        raise DemoUnavailable("The live demo isn't switched on yet (no AI model key configured).", 503)
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
    if session is not None and _format_of(session.messages) not in (None, which):
        session = None  # provider switched since this conversation began: start fresh
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

    turn = _gemini_turn if which == "gemini" else _claude_turn
    messages, reply = turn(list(session.messages), text, language, now, ctx)
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


def _model_name(which: str) -> str:
    return get_settings().demo_model or DEFAULT_MODELS[which]


def _claude_turn(
    history: list[dict], text: str, language: str, now: datetime, ctx: CallContext
) -> tuple[list[dict], str]:
    s = get_settings()
    messages = history + [{"role": "user", "content": text}]
    for _ in range(MAX_MODEL_ROUNDS):
        resp = get_client("claude").beta.messages.create(
            model=_model_name("claude"),
            max_tokens=4096,
            system=_system(language, now),
            tools=anthropic_tools(),
            messages=messages,
            output_config={"effort": s.demo_effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            return history, REFUSAL_REPLY  # the refused exchange stays out of the model's history
        messages.append({"role": "assistant", "content": _block_dicts(resp.content)})
        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            return messages, _text(resp.content)
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
    return messages, TROUBLE_REPLY


def _gemini_turn(
    history: list[dict], text: str, language: str, now: datetime, ctx: CallContext
) -> tuple[list[dict], str]:
    from google.genai import types

    s = get_settings()
    config = types.GenerateContentConfig(
        system_instruction="\n\n".join(_system_parts(language, now)),
        tools=gemini_tools(),
        # We run the tools ourselves, in demo mode, and keep the history.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level=s.demo_effort.upper()),
        max_output_tokens=4096,
    )
    messages = history + [{"role": "user", "parts": [{"text": text}]}]
    for _ in range(MAX_MODEL_ROUNDS):
        resp = get_client("gemini").models.generate_content(
            model=_model_name("gemini"),
            contents=[types.Content.model_validate(m) for m in messages],
            config=config,
        )
        cand = (resp.candidates or [None])[0]
        reason = getattr(cand, "finish_reason", None)
        finish = getattr(reason, "value", reason) or ""
        if cand is None or str(finish) in GEMINI_BLOCKED:
            return history, REFUSAL_REPLY  # the refused exchange stays out of the model's history
        if cand.content is None or not cand.content.parts:
            log.warning("gemini returned no content (finish_reason=%s)", finish)
            return history, TROUBLE_REPLY
        # Store the model's turn exactly as returned: thought signatures must come back unchanged.
        messages.append({**cand.content.model_dump(mode="json", exclude_none=True), "role": "model"})
        calls = [p.function_call for p in cand.content.parts if p.function_call]
        if not calls:
            return messages, " ".join(p.text for p in cand.content.parts if p.text and not p.thought).strip()
        parts = []
        for fc in calls:
            content, _ = _run_tool(fc.name or "", fc.args or {}, ctx)
            part = types.Part(
                function_response=types.FunctionResponse(id=fc.id, name=fc.name, response=json.loads(content))
            )
            parts.append(part.model_dump(mode="json", exclude_none=True))
        messages.append({"role": "user", "parts": parts})
    return messages, TROUBLE_REPLY


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

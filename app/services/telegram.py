"""Telegram Bot API over raw httpx: handoffs with inline buttons, acks, alerts.

Rule: the caller's phone number is only ever sent to a person's PRIVATE chat
(the assigned designer, or the studio head when they take over). Front-desk
alerts never contain it.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.config import get_rules, get_settings
from app.services.crm import CRM, Designer, Lead

log = logging.getLogger(__name__)
API = "https://api.telegram.org"


class TelegramClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self.token = token
        self.http = httpx.Client(timeout=10, transport=transport)

    def _call(self, method: str, payload: dict) -> dict:
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        resp = self.http.post(f"{API}/bot{self.token}/{method}", json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data.get('description')}")
        return data["result"]

    def send(self, chat_id: str, text: str, buttons: list[list[tuple[str, str]]] | None = None):
        payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row] for row in buttons
                ]
            }
        return self._call("sendMessage", payload)

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def clear_buttons(self, chat_id: str | int, message_id: int) -> None:
        self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )


_client: TelegramClient | None = None


def get_telegram() -> TelegramClient:
    global _client
    if _client is None:
        _client = TelegramClient(get_settings().telegram_bot_token)
    return _client


def set_telegram(client: TelegramClient | None) -> None:
    global _client
    _client = client


# ---------- message templates ----------
def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%a %d %b, %I:%M %p") if dt else "not booked"


def handoff_text(lead: Lead, flags: list[str], phone: str | None, link: str, prefix: str = "") -> str:
    # NOTE: the spec's "Step 7" handoff template was not provided; this layout is a
    # stand-in. Replace with the approved template.
    lines = [
        f"{prefix}New enquiry - {lead.name or 'name not given'}",
        f"Phone: {phone}" if phone else None,
        f"Flags: {', '.join(flags)}" if flags else None,
        f"Locality: {lead.locality or '-'}",
        f"Property: {lead.property_type or '-'} ({lead.project_category or '-'})",
        f"Rooms: {', '.join(lead.rooms) if lead.rooms else '-'}",
        f"Area: {int(lead.area_sqft)} sq ft" if lead.area_sqft else "Area: -",
        f"Service: {lead.service or '-'}",
        f"Completion: {lead.completion or 'flexible / not given'}",
        f"Decision-maker: {lead.decision_maker or 'unclear'}",
        f"Budget (caller volunteered): {int(lead.budget_inr)}" if lead.budget_inr else None,
        f"Referral: {lead.referral_source}" if lead.referral_source else None,
        f"Consultation: {lead.consult_type or '-'} {_fmt_dt(lead.consult_at)}",
        f"Open question: {lead.open_question}" if lead.open_question else None,
        f"Transcript: {link}",
    ]
    return "\n".join(line for line in lines if line)


HANDOFF_BUTTONS = [("Accept", "a"), ("Calling now", "c"), ("Reassign", "r")]
ACTIONS = {"a": "accept", "c": "calling_now", "r": "reassign"}


def escalate_urgent(phone: str | None, name: str | None, summary: str, link: str) -> None:
    minutes = get_rules().routing.urgent_escalation_callback_minutes
    text = (
        f"URGENT - existing client needs a call back within {minutes} min\n"
        f"Name: {name or '-'}\nPhone: {phone or 'not captured'}\nSummary: {summary}\n"
        f"Transcript: {link}"
    )
    get_telegram().send(get_settings().telegram_studio_head_chat_id, text)


def alert_front_desk(text: str) -> None:
    get_telegram().send(get_settings().telegram_front_desk_chat_id, text)


def send_consult_followup(designer: Designer, lead: Lead) -> None:
    get_telegram().send(
        designer.telegram_chat_id,
        f"How did the consultation with {lead.name or 'the client'} "
        f"({lead.locality or '-'}, {_fmt_dt(lead.consult_at)}) go?",
        [
            [("Consultation held", f"o:{lead.id}:h"), ("No-show", f"o:{lead.id}:n")],
            [("Won", f"o:{lead.id}:w"), ("Lost", f"o:{lead.id}:l")],
        ],
    )


# ---------- callbacks ----------
def handle_callback(crm: CRM, update: dict, at: datetime) -> str:
    """Process a callback_query update. Returns a short status for logging/tests."""
    cq = update.get("callback_query")
    if not cq:
        return "ignored"
    data = cq.get("data") or ""
    who = (cq.get("from") or {}).get("username") or str((cq.get("from") or {}).get("id"))
    msg = cq.get("message") or {}
    kind, _, rest = data.partition(":")
    ref, _, code = rest.rpartition(":")
    tg = get_telegram()

    if kind == "h" and code in ACTIONS:
        handoff = crm.get_handoff(ref)
        if not handoff:
            tg.answer_callback(cq["id"], "Handoff not found")
            return "not_found"
        crm.ack_handoff(ref, who, ACTIONS[code], at)
        if code == "r":
            from app.services import notify  # notify imports this module

            notify.reassign_requested(crm, handoff)
        tg.answer_callback(cq["id"], f"Recorded: {ACTIONS[code].replace('_', ' ')}")
        if msg.get("message_id") and code != "r":
            try:
                tg.clear_buttons(msg["chat"]["id"], msg["message_id"])
            except Exception:  # buttons are cosmetic; the DB write already happened
                log.warning("could not clear handoff buttons", exc_info=True)
        return ACTIONS[code]

    if kind == "o" and code in {"h", "n", "w", "l"}:
        field = {"h": ("held", True), "n": ("held", False), "w": ("won", True), "l": ("won", False)}[code]
        crm.upsert_consult_outcome(ref, **{field[0]: field[1]})
        tg.answer_callback(cq["id"], "Recorded")
        return f"{field[0]}={field[1]}"

    tg.answer_callback(cq["id"], "Unknown action")
    return "unknown"

"""Delivers handoffs over every channel a person has: Telegram and/or email.

A handoff counts as sent when at least one channel delivered it; if none did, the caller
of these functions gets an exception and reports the failure (the tool returns sent=false).
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.config import get_settings
from app.services import email, telegram
from app.services.crm import CRM, Designer, Handoff, Lead

log = logging.getLogger(__name__)


def _subject(lead: Lead | None, prefix: str = "") -> str:
    who = (lead.name if lead else None) or "new enquiry"
    where = f" - {lead.locality}" if lead and lead.locality else ""
    return f"{prefix}Aangan handoff: {who}{where}"


def send_handoff(
    crm: CRM, designer: Designer, lead: Lead, flags: list[str], phone: str | None, link: str, at: datetime
) -> Handoff:
    handoff = crm.create_handoff(lead.id, at)
    text = telegram.handoff_text(lead, flags, phone, link)
    sent = []
    if designer.telegram_chat_id and get_settings().telegram_bot_token:
        try:
            telegram.get_telegram().send(
                designer.telegram_chat_id,
                text,
                [[(label, f"h:{handoff.id}:{code}") for label, code in telegram.HANDOFF_BUTTONS]],
            )
            sent.append("telegram")
        except Exception:
            log.exception("telegram handoff failed for %s", designer.id)
    if designer.email and email.configured():
        try:
            email.send_handoff(designer.email, _subject(lead), text, handoff.id)
            sent.append("email")
        except Exception:
            log.exception("email handoff failed for %s", designer.id)
    if not sent:
        raise RuntimeError(f"handoff {handoff.id} not delivered: no working channel for {designer.id}")
    return handoff


def reassign_requested(crm: CRM, handoff: Handoff) -> None:
    lead = crm.get_lead(handoff.lead_id)
    to_studio_head(
        f"Reassign requested for lead {handoff.lead_id} "
        f"({lead.locality if lead else '-'}). Please assign another designer.",
        lead,
        prefix="REASSIGN: ",
    )


def to_studio_head(text: str, lead: Lead | None = None, prefix: str = "") -> None:
    """Re-routes and reassign requests: the studio head's Telegram and/or email."""
    s = get_settings()
    sent = []
    if s.telegram_studio_head_chat_id and s.telegram_bot_token:
        try:
            telegram.get_telegram().send(s.telegram_studio_head_chat_id, text)
            sent.append("telegram")
        except Exception:
            log.exception("telegram to studio head failed")
    if s.studio_head_email and email.configured():
        try:
            email.get_email().send(s.studio_head_email, _subject(lead, prefix), text)
            sent.append("email")
        except Exception:
            log.exception("email to studio head failed")
    if not sent:
        raise RuntimeError("studio head not reachable on any channel")

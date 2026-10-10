"""SMS behind a small interface; provider chosen by SMS_PROVIDER.

India: promotional/transactional SMS to Indian numbers requires DLT registration
(sender ID + approved template) with your provider. Register the confirmation
template below before go-live or messages will be blocked by the operator.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> bool: ...


class ConsoleSms:
    """Development: logs the message with the number masked."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> bool:
        self.sent.append((to, body))
        log.info("SMS to ***%s: %s", to[-3:], body)
        return True


class TwilioSms:
    def __init__(
        self, sid: str, token: str, sender: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.sid, self.sender = sid, sender
        self.http = httpx.Client(auth=(sid, token), timeout=10, transport=transport)

    def send(self, to: str, body: str) -> bool:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        try:
            resp = self.http.post(url, data={"To": to, "From": self.sender, "Body": body})
            if resp.status_code >= 300:
                log.error("twilio sms failed: %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except httpx.HTTPError:
            log.exception("twilio sms error")
            return False


_sender: SmsSender | None = None


def get_sms() -> SmsSender:
    global _sender
    if _sender is None:
        s = get_settings()
        if s.sms_provider == "twilio":
            _sender = TwilioSms(s.twilio_account_sid, s.twilio_auth_token, s.twilio_from_number)
        else:
            _sender = ConsoleSms()
    return _sender


def set_sms(sender: SmsSender | None) -> None:
    global _sender
    _sender = sender


def confirmation_text(label: str, consult_type: str, address: str) -> str:
    place = "at your site" if consult_type == "site" else f"at our studio, {address}"
    return (
        f"Aangan Studio: your free design consultation is confirmed for {label} {place}. "
        "Reply or call us to reschedule."
    )

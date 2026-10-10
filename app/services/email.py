"""Email over Resend's HTTP API, for designer handoffs.

An emailed handoff carries signed links (Accept / Calling now / Reassign) so the designer
can acknowledge it like the Telegram buttons; otherwise the re-route job would treat every
emailed handoff as ignored. Each link opens a confirm page and only the button on that
page records the action, so mail scanners that pre-fetch links can't acknowledge anything.

Same privacy rule as Telegram: the caller's phone only goes to one person's own inbox.
"""

from __future__ import annotations

import hashlib
import hmac
from html import escape

import httpx

from app.config import get_settings

API = "https://api.resend.com/emails"
ACK_ACTIONS = {"accept": "Accept", "calling_now": "Calling now", "reassign": "Reassign"}


class EmailClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self.api_key = api_key
        self.http = httpx.Client(timeout=10, transport=transport)

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> str:
        if not self.api_key:
            raise RuntimeError("RESEND_API_KEY is not set")
        payload = {"from": get_settings().email_from, "to": [to], "subject": subject, "text": text}
        if html:
            payload["html"] = html
        resp = self.http.post(API, json=payload, headers={"Authorization": f"Bearer {self.api_key}"})
        if resp.status_code >= 300:
            raise RuntimeError(f"resend failed: {resp.status_code} {resp.text[:300]}")
        return resp.json().get("id", "")


_client: EmailClient | None = None


def get_email() -> EmailClient:
    global _client
    if _client is None:
        _client = EmailClient(get_settings().resend_api_key)
    return _client


def set_email(client: EmailClient | None) -> None:
    global _client
    _client = client


def configured() -> bool:
    return bool(get_settings().resend_api_key)


# ---------- acknowledge links ----------
def ack_token(handoff_id: str, action: str) -> str:
    key = get_settings().tool_webhook_secret
    if not key:
        raise RuntimeError("TOOL_WEBHOOK_SECRET is not set")
    msg = f"handoff-ack:{handoff_id}:{action}".encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()[:32]


def ack_token_ok(handoff_id: str, action: str, token: str | None) -> bool:
    if action not in ACK_ACTIONS or not token:
        return False
    try:
        return hmac.compare_digest(token.encode(), ack_token(handoff_id, action).encode())
    except RuntimeError:
        return False


def ack_url(handoff_id: str, action: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/handoffs/{handoff_id}/{action}?t={ack_token(handoff_id, action)}"


# ---------- messages ----------
def send_handoff(to: str, subject: str, text: str, handoff_id: str) -> str:
    links = {action: ack_url(handoff_id, action) for action in ACK_ACTIONS}
    plain = (
        text
        + "\n\nLet the studio know:\n"
        + "\n".join(f"{label}: {links[action]}" for action, label in ACK_ACTIONS.items())
    )
    buttons = " ".join(
        f'<a href="{escape(links[action])}" style="display:inline-block;margin:0 8px 8px 0;'
        f"padding:10px 16px;border-radius:6px;background:#111;color:#fff;text-decoration:none;"
        f'font:600 14px sans-serif">{label}</a>'
        for action, label in ACK_ACTIONS.items()
    )
    html = (
        '<div style="font:15px/1.6 sans-serif;color:#2f3437">'
        f'<pre style="font:15px/1.6 sans-serif;white-space:pre-wrap;margin:0 0 20px">{escape(text)}</pre>'
        f"{buttons}</div>"
    )
    return get_email().send(to, subject, plain, html)

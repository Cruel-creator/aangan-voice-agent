"""Designer handoffs by email (Resend), with signed acknowledge links."""

import json
import re

import httpx
import pytest

from app.config import get_settings
from app.services import email
from tests.test_api import CALLER, LEAD, call_tool


class FakeEmail(email.EmailClient):
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer re_test"
            if fail:
                return httpx.Response(403, json={"message": "domain not verified"})
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"em_{len(self.sent)}"})

        super().__init__("re_test", transport=httpx.MockTransport(handler))


@pytest.fixture
def mail(world, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("STUDIO_HEAD_EMAIL", "head@studio.test")
    get_settings.cache_clear()
    fake = FakeEmail()
    email.set_email(fake)
    yield fake
    email.set_email(None)


def _email_only(world, designer_id="d-west"):
    world.crm.db.update(
        "designers", {"id": designer_id}, {"telegram_chat_id": None, "email": "west@studio.test"}
    )


def _handoff_by_email(world, mail):
    _email_only(world)
    assert call_tool(world, "send_handoff", LEAD) == {"sent": True}
    msg = mail.sent[-1]
    links = dict(re.findall(r"(Accept|Calling now|Reassign): (\S+)", msg["text"]))
    return msg, links


def test_email_only_designer_gets_handoff_with_ack_links(world, mail):
    msg, links = _handoff_by_email(world, mail)
    assert msg["to"] == ["west@studio.test"] and world.tg.sent == []
    assert msg["subject"] == "Aangan handoff: Priya - Wakad"
    assert CALLER in msg["text"] and "/dashboard/calls/" in msg["text"]
    assert set(links) == {"Accept", "Calling now", "Reassign"}
    assert all(u.startswith("https://agent.example.test/handoffs/") for u in links.values())
    assert 'href="https://agent.example.test/handoffs/' in msg["html"]


def test_designer_with_both_channels_gets_both(world, mail):
    world.crm.db.update("designers", {"id": "d-west"}, {"email": "west@studio.test"})
    assert call_tool(world, "send_handoff", LEAD) == {"sent": True}
    assert world.tg.sent[-1]["chat_id"] == "101" and mail.sent[-1]["to"] == ["west@studio.test"]


def test_one_working_channel_is_enough_but_none_is_a_failure(world, mail, monkeypatch):
    world.crm.db.update("designers", {"id": "d-west"}, {"email": "west@studio.test"})
    email.set_email(FakeEmail(fail=True))
    assert call_tool(world, "send_handoff", LEAD) == {"sent": True}  # Telegram still delivered
    _email_only(world)
    assert call_tool(world, "send_handoff", LEAD, call_id="call-uuid-2") == {"sent": False}


def test_link_opens_confirm_page_and_only_the_button_records(world, mail):
    _, links = _handoff_by_email(world, mail)
    path = links["Calling now"].removeprefix("https://agent.example.test")
    handoff_id = path.split("/")[2]
    page = world.client.get(path)
    assert page.status_code == 200 and "<form" in page.text
    assert world.crm.get_handoff(handoff_id).acked_at is None  # a GET (mail scanner) records nothing
    token = path.split("t=")[1]
    done = world.client.post(path.split("?")[0], data={"t": token})
    assert done.status_code == 200 and "Recorded" in done.text
    handoff = world.crm.get_handoff(handoff_id)
    assert handoff.action == "calling_now" and handoff.acked_by == "email"


def test_tampered_or_mismatched_links_are_rejected(world, mail):
    _, links = _handoff_by_email(world, mail)
    path = links["Accept"].removeprefix("https://agent.example.test")
    base, token = path.split("?t=")
    assert world.client.get(f"{base}?t=0000").status_code == 404
    other_action = base.replace("/accept", "/reassign")
    assert world.client.post(other_action, data={"t": token}).status_code == 404  # token is per action
    assert world.client.get(base).status_code == 404


def test_reassign_by_email_tells_studio_head(world, mail):
    _, links = _handoff_by_email(world, mail)
    path = links["Reassign"].removeprefix("https://agent.example.test")
    world.client.post(path.split("?")[0], data={"t": path.split("t=")[1]})
    assert world.tg.sent[-1]["chat_id"] == "999" and "Reassign requested" in world.tg.sent[-1]["text"]
    assert mail.sent[-1]["to"] == ["head@studio.test"] and mail.sent[-1]["subject"].startswith("REASSIGN")


def test_reroute_reaches_studio_head_by_email_without_telegram(world, mail, monkeypatch):
    from app.config import get_rules

    _handoff_by_email(world, mail)
    monkeypatch.delenv("TELEGRAM_STUDIO_HEAD_CHAT_ID")
    get_settings.cache_clear()
    monkeypatch.setattr(get_rules().routing, "handoff_ack_reroute_hours", 0.01)
    world.advance(seconds=40)
    cron = {"X-Cron-Secret": "cron-secret"}
    assert world.client.post("/cron/reroute-handoffs", headers=cron).json() == {"rerouted": 1}
    assert mail.sent[-1]["to"] == ["head@studio.test"]
    assert mail.sent[-1]["subject"].startswith("RE-ROUTED") and CALLER in mail.sent[-1]["text"]

"""Shared fakes. No test touches the network, Google or Telegram.

Only tests/test_postgres.py uses a real database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import calendar, clock, sms, telegram
from app.services.crm import CRM, Designer, MemoryBackend, set_crm

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 3, 11, 0, tzinfo=IST)  # a Thursday, inside office hours
SECRET = "test-tool-secret"


class FakeTelegram(telegram.TelegramClient):
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            payload = json.loads(request.content)
            self.calls.append((method, payload))
            if method == "sendMessage":
                self.sent.append(payload)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        super().__init__("TEST-TOKEN", transport=httpx.MockTransport(handler))


class FakeCalendar:
    def __init__(self) -> None:
        self.events: dict[str, list[dict]] = {}
        self.fail_inserts = 0
        self.on_insert = None
        self._n = 0

    def add(self, cal, start, end, created="2000-01-01T00:00:00Z"):
        self._n += 1
        ev = {"id": f"ev{self._n}", "start": start, "end": end, "created": created}
        self.events.setdefault(cal, []).append(ev)
        return ev

    def busy(self, cal, start, end):
        return [
            (e["start"], e["end"]) for e in self.events.get(cal, []) if e["start"] < end and e["end"] > start
        ]

    def insert(self, cal, start, end, summary, description):
        if self.fail_inserts:
            self.fail_inserts -= 1
            raise RuntimeError("google 500")
        ev = self.add(cal, start, end, created="2026-09-03T05:30:00.500Z")
        if self.on_insert:
            self.on_insert(cal, start, end)
        return ev["id"]

    def overlapping(self, cal, start, end):
        return [
            {"id": e["id"], "created": e["created"]}
            for e in self.events.get(cal, [])
            if e["start"] < end and e["end"] > start
        ]

    def delete(self, cal, event_id):
        self.events[cal] = [e for e in self.events[cal] if e["id"] != event_id]


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """Settings also read .env.local (written by `neon link`, holds the production URL).
    Env vars beat dotenv files, so point them at an unreachable host (blank values are
    ignored by Settings and would fall through to .env.local). No test may reach a real
    database by accident; tests/test_postgres.py connects only through TEST_DATABASE_URL."""
    from app.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)  # tests never read .env/.env.local
    for key in ("DATABASE_URL", "DATABASE_URL_UNPOOLED", "DATABASE_URL_DIRECT"):
        monkeypatch.setenv(key, "postgresql://tests-must-not-use-a-real-db.invalid/x")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch):
    values = {
        "TOOL_WEBHOOK_SECRET": SECRET,
        "PHONE_HASH_KEY": "test-hash-key",
        "PHONE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "TELEGRAM_BOT_TOKEN": "TEST-TOKEN",
        "TELEGRAM_WEBHOOK_SECRET": "tg-secret",
        "TELEGRAM_STUDIO_HEAD_CHAT_ID": "999",
        "TELEGRAM_FRONT_DESK_CHAT_ID": "-100555",
        "DASHBOARD_USER": "nikhil",
        "DASHBOARD_PASSWORD": "pw",
        "CRON_SECRET": "cron-secret",
        "PUBLIC_BASE_URL": "https://agent.example.test",
        "STUDIO_ADDRESS": "Test Studio Road",
        "USD_INR_RATE": "85",
        "TELEPHONY_COST_INR_PER_MIN": "1",
        "FIXED_MONTHLY_COST_INR": "3000",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def world(env, monkeypatch):
    """App wired to in-memory fakes, with a fixed clock."""
    crm = CRM(MemoryBackend())
    set_crm(crm)
    tg, cal, sender = FakeTelegram(), FakeCalendar(), sms.ConsoleSms()
    telegram.set_telegram(tg)
    calendar.set_calendar(cal)
    sms.set_sms(sender)
    state = {"now": NOW}
    monkeypatch.setattr(clock, "now", lambda: state["now"])
    designers = [
        crm.db.insert(
            "designers",
            Designer(
                id="d-west",
                name="West",
                telegram_chat_id="101",
                calendar_id="west@cal",
                areas=["Baner", "Wakad", "Kothrud"],
                handles_commercial=True,
            ).model_dump(),
        ),
        crm.db.insert(
            "designers",
            Designer(
                id="d-east",
                name="East",
                telegram_chat_id="102",
                calendar_id="east@cal",
                areas=["Kharadi", "Viman Nagar", "Baner"],
                handles_commercial=False,
            ).model_dump(),
        ),
    ]
    from app.main import app

    class World:
        pass

    w = World()
    w.crm, w.tg, w.cal, w.sms, w.state, w.designers = crm, tg, cal, sender, state, designers
    w.client = TestClient(app)
    w.advance = lambda **kw: state.__setitem__("now", state["now"] + timedelta(**kw))
    yield w
    set_crm(None)
    telegram.set_telegram(None)
    calendar.set_calendar(None)
    sms.set_sms(None)

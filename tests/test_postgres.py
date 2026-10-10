"""Integration tests against a REAL Postgres (local, or a Neon test branch).

Skipped unless TEST_DATABASE_URL is set (environment or the git-ignored .env). Each run
creates a throwaway schema, applies db/migrations through scripts/migrate.py, exercises
PostgresBackend + CRM + the full HTTP flow, then drops the schema. Use a direct
(non-pooler) URL, ideally a Neon branch other than production.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from dotenv import dotenv_values

from app.config import ROOT

URL = os.environ.get("TEST_DATABASE_URL") or dotenv_values(ROOT / ".env").get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=IST)


def _with_schema(url: str, schema: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(url)
    params["options"] = f"-c search_path={schema}"
    return make_conninfo(**params)


@pytest.fixture
def pg():
    import psycopg

    from app.services.crm import CRM, PostgresBackend
    from scripts.migrate import apply

    schema = f"t_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f"create schema {schema}")
    url = _with_schema(URL, schema)
    applied = apply(url, seed=True)
    backend = PostgresBackend(url, max_size=2)
    yield CRM(backend), applied, url
    backend.pool.close()
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f"drop schema {schema} cascade")


def test_migrations_apply_once_and_seed_loads(pg):
    from scripts.migrate import apply

    crm, applied, url = pg
    assert applied == ["20261007000000_init.sql", "seed.sql"]
    assert apply(url) == []  # idempotent
    designers = crm.list_designers()
    assert len(designers) == 3 and all(len(d.id) == 36 for d in designers)


def test_types_round_trip(pg):
    from app.services.crm import CallRecord, Lead

    crm, _, _ = pg
    call = crm.upsert_call(
        CallRecord(
            platform_call_id="c1",
            flags=["TIGHT"],
            criteria={"scope": "pass"},
            started_at=NOW,
            cost_inr=12.5,
        )
    )
    assert call.flags == ["TIGHT"] and call.criteria == {"scope": "pass"}
    assert call.started_at == NOW and call.cost_inr == 12.5 and isinstance(call.id, str)

    merged = crm.upsert_call(CallRecord(platform_call_id="c1", outcome="QUALIFIED", flags=[]))
    assert merged.flags == [] and merged.criteria == {"scope": "pass"} and merged.outcome == "QUALIFIED"

    lead = crm.create_lead(Lead(call_id=call.id, rooms=[], status="booked", consult_at=NOW))
    assert lead.rooms == [] and lead.created_at is not None
    assert [x.id for x in crm.leads_for_calls([call.id])] == [lead.id]
    assert crm.leads_due_followup(NOW + timedelta(hours=1))[0].id == lead.id
    assert crm.calls_between(NOW - timedelta(days=1), NOW + timedelta(days=1))[0].id == call.id


def test_handoffs_outcomes_audits_and_lookup(pg):
    from app.services.crm import Audit, CallRecord, Lead

    crm, _, _ = pg
    call = crm.upsert_call(CallRecord(platform_call_id="c2", phone_hash="h"))
    lead = crm.create_lead(Lead(call_id=call.id))
    h = crm.create_handoff(lead.id, NOW - timedelta(hours=3))
    assert [x.id for x in crm.unacked_handoffs(NOW - timedelta(hours=2))] == [h.id]
    crm.ack_handoff(h.id, "aryan", "accept", NOW)
    assert crm.unacked_handoffs(NOW) == []
    crm.upsert_consult_outcome(lead.id, held=True)
    crm.upsert_consult_outcome(lead.id, won=True, project_value_inr=900000)
    out = crm.outcomes_for_leads([lead.id])[0]
    assert (out.held, out.won, out.project_value_inr) == (True, True, 900000)
    assert crm.lookup_caller("h") == (True, crm.get_lead(lead.id))
    assert crm.lookup_caller("h", exclude_call_id=call.id) == (False, None)
    crm.add_audit(Audit(call_id=call.id, auditor="n", agrees=True, audited_at=NOW))
    assert len(crm.audits_between(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))) == 1


def test_full_http_flow_on_postgres(pg, world):
    """Same app, same fakes for Google/Telegram/SMS, but the CRM is real Postgres."""
    from app.services.crm import Designer, set_crm

    crm, _, _ = pg
    set_crm(crm)
    crm.db.insert(
        "designers",
        Designer(
            id="00000000-0000-0000-0000-0000000000aa",
            name="West",
            telegram_chat_id="101",
            calendar_id="west@cal",
            areas=["Wakad"],
            handles_commercial=True,
        ).model_dump(exclude={"last_assigned_at"}),
    )
    from tests.test_api import AUTH, LEAD, call_tool, eoc

    slots = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})["slots"]
    booked = call_tool(
        world,
        "book_consultation",
        {
            **LEAD,
            "completion_date": "March",
            "slot": slots[0]["start"],
            "designer_id": slots[0]["designer_id"],
        },
    )
    assert booked["confirmed"] is True
    assert call_tool(world, "send_handoff", {**LEAD, "completion_date": "March"}) == {"sent": True}
    assert call_tool(world, "log_call", {**LEAD, "completion_date": "March"}) == {"logged": True}
    resp = world.client.post("/webhooks/end-of-call", json=eoc(), headers=AUTH)
    assert resp.status_code == 200
    call = crm.get_call_by_platform_id("call-uuid-1")
    assert call.outcome == "QUALIFIED" and "PRICING_ASKED" in call.flags and call.logged_by_agent

    hid = crm.db.select("handoffs")[0]["id"]
    r = world.client.post(
        "/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "callback_query": {
                "id": "1",
                "data": f"h:{hid}:a",
                "from": {"username": "w"},
                "message": {"message_id": 1, "chat": {"id": 101}},
            }
        },
    )
    assert r.json()["status"] == "accept" and crm.get_handoff(hid).action == "accept"

    page = world.client.get(f"/dashboard/calls/{call.id}", auth=("nikhil", "pw"))
    assert page.status_code == 200
    assert world.client.get("/dashboard/calls/not-a-uuid", auth=("nikhil", "pw")).status_code == 404
    made_up = call_tool(
        world, "book_consultation", {**LEAD, "slot": slots[1]["start"], "designer_id": "designer-1"}
    )
    assert made_up["confirmed"] is False and made_up["reason"] == "unknown_designer"
    m = world.client.get("/dashboard/api/metrics?start=2026-09-01&end=2026-09-30", auth=("nikhil", "pw"))
    assert m.json()["funnel"]["booked"] == 1

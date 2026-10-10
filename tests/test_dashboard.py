from datetime import datetime, timedelta

from app.dashboard import metrics
from app.services.crm import Audit, CallRecord, Lead
from tests.conftest import IST

T = datetime(2026, 9, 10, 12, 0, tzinfo=IST)


def seed(crm):
    def call(pid, intent, outcome, office, pickup=None, cost=0.0, leak=False):
        return crm.upsert_call(
            CallRecord(
                platform_call_id=pid,
                intent=intent,
                outcome=outcome,
                in_office_hours=office,
                pickup_seconds=pickup,
                cost_inr=cost,
                price_leak=leak,
                started_at=T,
            )
        )

    c1 = call("c1", "new", "QUALIFIED", True, 2, 10)
    call("c2", "new", "DECLINE_AREA", False, 400, 5)
    c3 = call("c3", "follow_up", "QUALIFIED", False, None, 8)
    call("c4", "existing_client_complaint", "ESCALATE_URGENT", True, None, 4, leak=True)
    c5 = call("c5", "new", "HUMAN_REVIEW", True, None, 3)
    crm.upsert_call(CallRecord(platform_call_id="c6", intent="other", started_at=T))
    crm.upsert_call(
        CallRecord(
            platform_call_id="old",
            intent="new",
            outcome="QUALIFIED",
            started_at=T - timedelta(days=60),
            cost_inr=999,
        )
    )

    l1 = crm.create_lead(Lead(call_id=c1.id, booking_id="b1", status="booked"))
    l3 = crm.create_lead(Lead(call_id=c3.id, booking_id="b3", status="booked"))
    crm.create_lead(Lead(call_id=c5.id, status="human_review"))
    h1 = crm.create_handoff(l1.id, T)
    crm.ack_handoff(h1.id, "a", "accept", T + timedelta(minutes=30))
    h3 = crm.create_handoff(l3.id, T)
    crm.ack_handoff(h3.id, "b", "accept", T + timedelta(minutes=10))
    crm.upsert_consult_outcome(l1.id, held=True, won=True, project_value_inr=1_000_000)
    crm.upsert_consult_outcome(l3.id, held=True, won=False)
    for agrees in (True, True, False):
        crm.add_audit(Audit(call_id=c1.id, auditor="x", agrees=agrees, audited_at=T))


def test_metrics_match_hand_calculation(world):
    seed(world.crm)
    m = metrics.compute(world.crm, datetime(2026, 9, 1, tzinfo=IST), datetime(2026, 10, 1, tzinfo=IST))
    assert m["funnel"] == {"calls": 6, "enquiries": 4, "qualified": 2, "booked": 2, "held": 2, "won": 1}
    assert m["speed"] == {
        "pct_answered_under_5_min": 50.0,
        "median_pickup_seconds": 201,
        "after_hours_share_pct": 40.0,
        "median_minutes_to_designer_ack": 20.0,
    }
    assert m["declines"] == {"DECLINE_AREA": 1}
    q = m["quality"]
    assert (
        q["human_review_rate_pct"],
        q["escalations"],
        q["price_leak_flags"],
        q["unlogged_calls"],
        q["audit_agreement_pct"],
    ) == (25.0, 1, 1, 1, 66.7)
    c = m["cost"]
    # variable 30 + fixed 3000 (30-day window = 1 month)
    assert (c["total_inr"], c["per_call_inr"], c["per_booked_consultation_inr"]) == (3030, 505, 1515)
    assert (c["win_rate_pct"], c["avg_project_value_inr"], c["pipeline_value_inr"]) == (
        50.0,
        1_000_000,
        1_000_000,
    )


def test_empty_window_reports_no_data_not_zero(world):
    m = metrics.compute(world.crm, datetime(2026, 1, 1, tzinfo=IST), datetime(2026, 1, 31, tzinfo=IST))
    assert m["speed"]["median_pickup_seconds"] is None
    assert m["cost"]["per_call_inr"] is None and m["cost"]["pipeline_value_inr"] is None


def test_dashboard_page_and_api(world):
    seed(world.crm)
    page = world.client.get("/dashboard", auth=("nikhil", "pw"))
    assert page.status_code == 200 and "chart.umd.min.js" in page.text
    api = world.client.get("/dashboard/api/metrics?start=2026-09-01&end=2026-09-30", auth=("nikhil", "pw"))
    assert api.json()["funnel"]["calls"] == 6


def test_call_detail_and_audit_form(world):
    seed(world.crm)
    call_id = world.crm.get_call_by_platform_id("c1").id
    page = world.client.get(f"/dashboard/calls/{call_id}", auth=("nikhil", "pw"))
    assert page.status_code == 200 and "QUALIFIED" in page.text
    resp = world.client.post(
        f"/dashboard/calls/{call_id}/audit",
        auth=("nikhil", "pw"),
        data={"agrees": "false", "notes": "asked budget"},
    )
    assert resp.status_code == 200
    assert world.crm.db.tables["transcript_audits"][-1]["notes"] == "asked budget"

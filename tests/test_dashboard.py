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


def test_dashboard_page_and_api_are_public(world):
    seed(world.crm)
    page = world.client.get("/dashboard")
    assert page.status_code == 200 and "chart.umd.min.js" in page.text and "/assets/dashboard.js" in page.text
    api = world.client.get("/dashboard/api/metrics?start=2026-09-01&end=2026-09-30")
    assert api.json()["funnel"]["calls"] == 6


def test_demo_calls_are_excluded_unless_asked_for(world):
    seed(world.crm)
    world.crm.upsert_call(CallRecord(platform_call_id="web-demo-1", is_demo=True, started_at=T, intent="new"))
    q = "start=2026-09-01&end=2026-09-30"
    assert world.client.get(f"/dashboard/api/metrics?{q}").json()["funnel"]["calls"] == 6
    assert world.client.get(f"/dashboard/api/metrics?{q}&include_demo=true").json()["funnel"]["calls"] == 7
    rows = world.client.get(f"/dashboard/api/calls?{q}&include_demo=true").json()["calls"]
    assert len(rows) == 7 and sum(r["is_demo"] for r in rows) == 1
    assert all("phone" not in key for row in rows for key in row)  # nothing phone-related in the list


def test_home_page_is_the_receptionist(world):
    page = world.client.get("/")
    assert page.status_code == 200 and "/assets/demo.js" in page.text and "/dashboard" in page.text
    for secret in ("test-tool-secret", "cron-secret"):  # nothing sensitive on a public page
        assert secret not in page.text
    assert world.client.get("/assets/app.css").status_code == 200


def test_dashboard_script_fetches_by_absolute_path_and_explains_empty_state(world):
    js = world.client.get("/assets/dashboard.js").text
    common = world.client.get("/assets/common.js").text
    assert "new URL(path, location.origin)" in common  # works with any page URL shape
    demo_js = world.client.get("/assets/demo.js").text
    assert "No phone calls in this date range yet" in js
    assert ".innerHTML" not in js + common + demo_js  # all dynamic text goes in via textContent


def test_call_detail_api_masks_phone_numbers_and_emails(world):
    seed(world.crm)
    call = world.crm.get_call_by_platform_id("c1")
    world.crm.upsert_call(
        CallRecord(
            platform_call_id="c1",
            transcript=(
                "AI: Hello\nCaller: Call me on +91 98220 12345 or priya@example.com, site visit 2026-10-12"
            ),
        )
    )
    page = world.client.get(f"/dashboard/calls/{call.id}")
    assert page.status_code == 200 and "/assets/common.js" in page.text
    data = world.client.get(f"/dashboard/api/calls/{call.id}").json()
    text = " ".join(line["text"] for line in data["transcript"])
    assert "98220" not in text and "priya@example.com" not in text
    assert "[number hidden]" in text and "[email hidden]" in text and "2026-10-12" in text
    assert [line["speaker"] for line in data["transcript"]] == ["agent", "caller"]
    assert world.client.get("/dashboard/api/calls/not-a-uuid").status_code == 404


def test_audit_can_be_saved_and_is_listed(world):
    seed(world.crm)
    call_id = world.crm.get_call_by_platform_id("c1").id
    resp = world.client.post(
        f"/dashboard/api/calls/{call_id}/audit",
        json={"agrees": False, "notes": "asked budget", "auditor": "Nikhil"},
    )
    assert resp.status_code == 200
    audits = world.client.get(f"/dashboard/api/calls/{call_id}").json()["audits"]
    assert {"agrees": False, "notes": "asked budget", "auditor": "Nikhil"}.items() <= audits[-1].items()

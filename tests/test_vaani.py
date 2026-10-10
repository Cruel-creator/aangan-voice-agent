"""Vaani custom-tool adapter: tolerant of how Vaani shapes the request."""

from tests.conftest import SECRET

AUTH = {"X-Webhook-Secret": SECRET}


def test_requires_the_shared_secret(world):
    assert world.client.post("/vaani/tools/check_service_area", json={}).status_code == 401
    bad = {"X-Webhook-Secret": "nope"}
    assert world.client.post("/vaani/tools/check_service_area", json={}, headers=bad).status_code == 401


def test_flat_body_with_call_metadata(world):
    body = {"locality": "Wakad", "call_id": "room-1", "phone_number": "+919822012345", "agent_id": "x"}
    resp = world.client.post("/vaani/tools/check_service_area", json=body, headers=AUTH)
    assert resp.status_code == 200 and resp.json() == {"result": "in"}


def test_arguments_object_query_ids_blanks_and_comma_lists(world):
    body = {
        "arguments": {
            "rooms": "kitchen, living room",
            "property_type": "",
            "area_sqft": None,
            "budget_inr": "null",
            "project_category": "residential",
        }
    }
    resp = world.client.post(
        "/vaani/tools/check_budget?call_id=room-2&caller_number=%2B919822012345", json=body, headers=AUTH
    )
    assert resp.status_code == 200 and resp.json() == {"result": "not_mentioned"}


def test_bad_arguments_come_back_as_readable_200(world):
    resp = world.client.post("/vaani/tools/get_slots", json={"consult_type": "zoom"}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["fields"] == ["consult_type"]


def test_calls_without_an_id_group_by_caller_and_hour(world):
    from app.platform import vaani
    from app.tools.handlers import TOOLS

    req = vaani.to_tool_request(
        TOOLS["lookup_caller"][0],
        {"phone": "+919822012345"},
        {},
        {"X-Caller-Number": "+919822012345"},
        __import__("datetime").datetime(2026, 10, 10, 18, 5),
    )
    assert req.call_id == "vaani-+919822012345-2026101018"
    assert req.caller_number == "+919822012345" and req.arguments == {"phone": "+919822012345"}


def test_unsubstituted_variables_are_ignored_and_reported_without_values():
    from app.platform import vaani
    from app.tools.handlers import TOOLS

    query = {"call_id": "{{room_name}}", "caller_number": "+919822012345", "x": ""}
    assert vaani.variable_report(query) == {"call_id": "literal", "caller_number": "filled", "x": "empty"}
    req = vaani.to_tool_request(
        TOOLS["check_service_area"][0],
        {"locality": "{{locality}}"},
        query,
        {},
        __import__("datetime").datetime(2026, 10, 10, 18),
    )
    assert req.call_id == "vaani-+919822012345-2026101018" and req.arguments == {}


def test_unknown_tool_404(world):
    assert world.client.post("/vaani/tools/nope", json={}, headers=AUTH).status_code == 404

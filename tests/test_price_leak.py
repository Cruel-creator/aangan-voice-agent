"""No pricing figure may be reachable from any agent-facing string."""

import json
import re
from pathlib import Path

import pytest

from app.config import PROMPT_PATH, get_rules
from app.tools.models import RESPONSE_MODELS
from scripts.export_tools import build_config
from tests.test_api import LEAD, call_tool

APP = Path(__file__).resolve().parent.parent / "app"
BANNED_WORDS = ["per sq ft", "lakh", "₹"]


def banned_numbers() -> set[str]:
    floors = get_rules().budget.floors_inr.model_dump()
    return {str(v) for v in floors.values()}


def assert_no_price(text: str, where: str) -> None:
    lowered = text.lower()
    for word in BANNED_WORDS:
        assert word not in lowered, f"{where} contains {word!r}"
    numbers = set(re.findall(r"\d+", text.replace(",", "")))
    leaked = numbers & banned_numbers()
    assert not leaked, f"{where} contains price floor {leaked}"


def test_system_prompt_has_no_price():
    assert_no_price(PROMPT_PATH.read_text(encoding="utf-8"), "system prompt")


def test_exported_agent_config_has_no_price(env):
    config = build_config("https://example.test")
    assert_no_price(json.dumps(config, ensure_ascii=False), "agent_config.json")


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_response_models_have_no_price(model):
    assert_no_price(json.dumps(model.model_json_schema(), ensure_ascii=False), model.__name__)


def test_every_tool_response_has_no_price(world):
    """Run every tool with realistic input and scan the raw strings the agent receives."""
    slots = call_tool(world, "get_slots", {"consult_type": "studio", "locality": "Wakad"})
    responses = [
        call_tool(world, "lookup_caller", {}),
        call_tool(
            world, "check_scope", {"scope_description": "full 2BHK redesign", "property_type": "apartment"}
        ),
        call_tool(world, "check_service_area", {"locality": "Wakad"}),
        call_tool(world, "check_timeline", {"completion_date": "November", "flexible": False}),
        call_tool(
            world,
            "check_budget",
            {"rooms": ["kitchen"], "property_type": "apartment", "area_sqft": 1800, "budget_inr": 350000},
        ),
        slots,
        call_tool(
            world,
            "book_consultation",
            {
                **LEAD,
                "budget_inr": 1200,
                "slot": slots["slots"][0]["start"],
                "designer_id": slots["slots"][0]["designer_id"],
            },
        ),
        call_tool(world, "send_handoff", {**LEAD, "budget_inr": 1800}),
        call_tool(world, "escalate_urgent", {"summary": "complaint"}),
        call_tool(world, "route_human_review", {**LEAD, "open_question": "x"}),
        call_tool(world, "log_call", {**LEAD, "budget_inr": 350000}),
    ]
    for r in responses:
        assert_no_price(json.dumps(r, ensure_ascii=False), f"tool response {r}")


def test_floors_are_only_read_in_budget_module():
    pattern = re.compile(r"floors_inr|per_room|residential_per_sqft|commercial_per_sqft")
    offenders = [
        str(p.relative_to(APP))
        for p in APP.rglob("*.py")
        if p.name not in {"budget.py", "config.py"} and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_assert_no_price_actually_catches_leaks():
    for bad in ["around 3.5 lakh", "₹ 50,000", "1800 per sq ft", "3,50,000", "costs 1,800"]:
        with pytest.raises(AssertionError):
            assert_no_price(bad, "probe")

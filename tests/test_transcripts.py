"""Every fixture case must reach its expected outcome and engine flags.

PRICING_ASKED and RESUMED_CALL are conversation-level flags: the fixture `facts`
carry no signal for them, so they are asserted in the simulated-call harness
(tests/sim/), not here.
"""

from pathlib import Path

import pytest
import yaml

from app.qualify.decision import CALL_FLAGS, ENGINE_FLAGS, Flag, LeadFacts, decide

CASES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "transcript_cases.yaml").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fixture_outcome_and_flags(case):
    decision = decide(LeadFacts(**case["facts"]), today=case["call_date"])
    expected_flags = {Flag(f) for f in case["expected"]["flags"]}

    assert decision.outcome == case["expected"]["outcome"], decision.criteria
    assert set(decision.flags) & ENGINE_FLAGS == expected_flags & ENGINE_FLAGS


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_call_flags_come_from_call_signals(case):
    expected = {Flag(f) for f in case["expected"]["flags"]} & CALL_FLAGS
    facts = LeadFacts(
        **case["facts"],
        pricing_asked=Flag.PRICING_ASKED in expected,
        resumed_call=Flag.RESUMED_CALL in expected,
    )
    decision = decide(facts, today=case["call_date"])
    if decision.outcome not in {"ESCALATE_URGENT"}:
        assert set(decision.flags) & CALL_FLAGS == expected

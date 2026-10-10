"""Criterion 4: is a volunteered budget clearly below what the scope costs?

This is the ONLY module that reads price floors. Its public function returns a
label, never a figure.
"""

from __future__ import annotations

from typing import Literal

from app.config import Rules, get_rules
from app.qualify.scope import is_commercial

BudgetResult = Literal["ok", "clearly_below", "not_mentioned"]

WHOLE_HOME_ROOMS = {"full_home", "whole_home", "full_house"}


def _floor_inr(
    rooms: list[str],
    property_type: str | None,
    project_category: str | None,
    area_sqft: float | None,
    rules: Rules,
) -> float | None:
    floors = rules.budget.floors_inr
    if is_commercial(property_type, project_category):
        return area_sqft * floors.commercial_per_sqft if area_sqft else None

    candidates: list[float] = []
    if area_sqft:
        candidates.append(area_sqft * floors.residential_per_sqft)
    named_rooms = [r for r in rooms if r.lower() not in WHOLE_HOME_ROOMS]
    if named_rooms and not any(r.lower() in WHOLE_HOME_ROOMS for r in rooms):
        candidates.append(len(named_rooms) * floors.per_room)
    # Lower of the applicable floors, so borderline cases still reach a designer.
    return min(candidates) if candidates else None


def check_budget(
    rooms: list[str] | None,
    property_type: str | None,
    area_sqft: float | None,
    budget_inr: float | None,
    project_category: str | None = None,
    rules: Rules | None = None,
) -> BudgetResult:
    r = rules or get_rules()
    if budget_inr is None:
        return "not_mentioned"
    floor = _floor_inr(rooms or [], property_type, project_category, area_sqft, r)
    if floor is None:
        return "ok"  # cannot judge the scope; let the designer discuss it
    return "clearly_below" if budget_inr < floor * r.budget.clearly_below_ratio else "ok"

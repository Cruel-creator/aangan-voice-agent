"""Criterion 1: is this a real design + execution project we take on?"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import Rules, get_rules

ScopeResult = Literal["pass", "fail", "unclear"]

COMMERCIAL_PROPERTY_TYPES = {"office", "commercial", "coworking", "clinic", "studio_office"}


@dataclass(frozen=True)
class ScopeCheck:
    result: ScopeResult
    reason: str
    needs_human_review: bool = False


def is_commercial(property_type: str | None, project_category: str | None) -> bool:
    if (project_category or "").lower() == "commercial":
        return True
    return (property_type or "").lower() in COMMERCIAL_PROPERTY_TYPES


def check_scope(
    property_type: str | None,
    project_category: str | None = None,
    service: str | None = None,
    area_sqft: float | None = None,
    description: str | None = None,
    rules: Rules | None = None,
) -> ScopeCheck:
    r = (rules or get_rules()).scope
    declined_types = {t.lower() for t in r.decline_project_types}

    for value in (property_type, project_category):
        if value and value.lower() in declined_types:
            return ScopeCheck("fail", f"project_type_not_served:{value.lower()}")

    if service and service.lower() in {s.lower() for s in r.decline_service_types}:
        return ScopeCheck("fail", f"service_not_offered:{service.lower()}")

    if not service and description:
        text = description.lower()
        hits = [s for s in r.advice_signals if s.lower() in text]
        if hits:
            # Free text only hints at advice-only work: ask once, don't decline on a keyword.
            return ScopeCheck("unclear", "confirm_design_and_execution_wanted")

    if is_commercial(property_type, project_category):
        if area_sqft is None:
            return ScopeCheck("unclear", "commercial_area_needed")
        if area_sqft > r.commercial.max_sqft:
            return ScopeCheck("fail", "commercial_above_max_area")
        if area_sqft < r.commercial.min_sqft:
            if r.commercial.below_min_action == "decline":
                return ScopeCheck("fail", "commercial_below_min_area")
            return ScopeCheck("unclear", "commercial_below_min_area_human_review", True)

    if not property_type and not project_category:
        return ScopeCheck("unclear", "property_type_needed")

    return ScopeCheck("pass", "in_scope")

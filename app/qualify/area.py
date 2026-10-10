"""Criterion 2: is the site inside Pune / PCMC service area?"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from app.config import Rules, get_rules

AreaResult = Literal["in", "out", "unclear"]

# Words callers add around a locality that carry no information.
_NOISE = re.compile(r"\b(pune|pcmc|maharashtra|india|near|area|road side|west|east)\b")


@dataclass(frozen=True)
class AreaCheck:
    result: AreaResult
    canonical: str | None = None


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    text = re.sub(r"(.)\1+", r"\1", text)  # "saudaagar" -> "saudagar"
    return re.sub(r"\s+", " ", text).strip()


def _squash(text: str) -> str:
    return text.replace(" ", "")


def _match(fragment: str, names: list[str], ratio: float) -> str | None:
    frag = _norm(_NOISE.sub(" ", fragment.lower()))
    if not frag:
        return None
    # Longest names first so "Talegaon Dabhade" wins over "Talegaon".
    ordered = sorted(names, key=len, reverse=True)
    for name in ordered:
        n = _norm(name)
        if _squash(frag) == _squash(n) or re.search(rf"\b{re.escape(n)}\b", frag):
            return name
    best, best_ratio = None, 0.0
    for name in ordered:
        score = SequenceMatcher(None, _squash(frag), _squash(_norm(name))).ratio()
        if score > best_ratio:
            best, best_ratio = name, score
    return best if best_ratio >= ratio else None


def resolve_locality(locality: str | None, rules: Rules | None = None) -> AreaCheck:
    r = (rules or get_rules()).service_area
    if not locality or not locality.strip():
        return AreaCheck("unclear")

    aliases = {_norm(k): v for k, v in r.aliases.items()}
    fragments = [f for f in re.split(r"[,/]", locality) if f.strip()] or [locality]

    for fragment in fragments:
        key = _norm(_NOISE.sub(" ", fragment.lower()))
        if key in aliases:
            return AreaCheck("in" if aliases[key] in r.allow else "out", aliases[key])
        denied = _match(fragment, r.deny, r.fuzzy_match_ratio)
        allowed = _match(fragment, r.allow, r.fuzzy_match_ratio)
        if denied and allowed:
            # Prefer the longer, more specific match; ties are ambiguous.
            if len(denied) == len(allowed):
                return AreaCheck("unclear")
            winner = denied if len(denied) > len(allowed) else allowed
            return AreaCheck("out" if winner == denied else "in", winner)
        if denied:
            return AreaCheck("out", denied)
        if allowed:
            return AreaCheck("in", allowed)
    return AreaCheck("unclear")


def check_service_area(locality: str | None, rules: Rules | None = None) -> AreaResult:
    return resolve_locality(locality, rules).result

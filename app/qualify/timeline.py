"""Criterion 3: is there enough time to design and execute before the deadline?

`completion` is when the caller needs the project DONE. A possession or
move-out date (site availability) is never passed here.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from app.config import Rules, get_rules

TimelineResult = Literal["ok", "tight", "too_soon", "unclear"]

MONTHS = {name.lower(): i for i in range(1, 13) for name in (calendar.month_name[i], calendar.month_abbr[i])}
MONTHS["sept"] = 9
NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


@dataclass(frozen=True)
class TimelineCheck:
    result: TimelineResult
    deadline: date | None = None
    weeks: float | None = None


def _end_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def parse_deadline(text: str, today: date) -> date | None:
    """Turn a caller's completion date into a calendar date, or None if unparseable."""
    s = text.strip().lower()
    s = re.sub(r"\b(by|before|end of|around|latest|done)\b", " ", s)
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()

    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{4})-(\d{2})", s):
        return _end_of_month(int(m[1]), int(m[2]))

    # Relative: "in 3 weeks", "within two months", "3 weeks from now"
    m = re.fullmatch(r"(?:in|within)?\s*(\d+|[a-z]+)\s+(day|week|month)s?(?:\s+from now)?", s)
    if m:
        n = int(m[1]) if m[1].isdigit() else NUMBER_WORDS.get(m[1])
        if n is None:
            return None
        if m[2] == "day":
            return today + timedelta(days=n)
        if m[2] == "week":
            return today + timedelta(weeks=n)
        return _add_months(today, n)
    if s == "next month":
        nxt = _add_months(today.replace(day=1), 1)
        return _end_of_month(nxt.year, nxt.month)

    # "15 march 2027", "march 15", "march 2027", "march"
    m = re.fullmatch(r"(?:(\d{1,2}) )?([a-z]+)(?: (\d{1,2}))?(?:,? (\d{4}))?", s)
    if m and m[2] in MONTHS:
        month = MONTHS[m[2]]
        day = int(m[1] or m[3]) if (m[1] or m[3]) else None
        if m[4]:
            year = int(m[4])
        else:
            # Next occurrence: this year if that month hasn't finished yet, else next year.
            year = today.year if _end_of_month(today.year, month) >= today else today.year + 1
        if day is None:
            return _end_of_month(year, month)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def check_timeline(
    completion: str | None,
    flexible: bool,
    today: date,
    rules: Rules | None = None,
) -> TimelineCheck:
    t = (rules or get_rules()).timeline
    if not completion or not str(completion).strip():
        return TimelineCheck("ok" if flexible else "unclear")

    deadline = parse_deadline(str(completion), today)
    if deadline is None:
        return TimelineCheck("unclear")
    weeks = (deadline - today).days / 7
    if weeks < 0:
        return TimelineCheck("unclear", deadline, weeks)  # past date: mis-heard, re-ask
    if weeks < t.too_soon_below_weeks:
        return TimelineCheck("too_soon", deadline, weeks)
    if weeks < t.tight_below_weeks:
        return TimelineCheck("tight", deadline, weeks)
    return TimelineCheck("ok", deadline, weeks)

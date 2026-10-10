"""The only place the app reads the wall clock. Logic receives `now`/`today` as arguments."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.config import get_rules


def tz() -> ZoneInfo:
    return ZoneInfo(get_rules().timezone)


def now() -> datetime:
    return datetime.now(tz())


def in_office_hours(at: datetime) -> bool:
    hours = get_rules().office_hours
    local: time = at.astimezone(tz()).time()
    return hours.start <= local < hours.end

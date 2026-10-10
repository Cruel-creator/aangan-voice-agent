"""Consultation slots and booking on designers' Google Calendars.

The Google client sits behind `CalendarGateway` so slot logic and the
double-booking guard are testable without Google.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.config import Rules, get_rules, get_settings
from app.qualify.area import resolve_locality
from app.services.crm import Designer

log = logging.getLogger(__name__)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class CalendarGateway(Protocol):
    def busy(self, calendar_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]: ...
    def insert(
        self, calendar_id: str, start: datetime, end: datetime, summary: str, description: str
    ) -> str: ...
    def overlapping(self, calendar_id: str, start: datetime, end: datetime) -> list[dict]: ...
    def delete(self, calendar_id: str, event_id: str) -> None: ...


class GoogleCalendarGateway:
    """Service account must be given "Make changes to events" on each designer calendar."""

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, service_account_json: str, timezone: str) -> None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        raw = service_account_json.strip()
        info = json.loads(raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8"))
        creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.tz = timezone

    def busy(self, calendar_id, start, end):
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": self.tz,
            "items": [{"id": calendar_id}],
        }
        cal = self.svc.freebusy().query(body=body).execute()["calendars"][calendar_id]
        if cal.get("errors"):
            raise RuntimeError(f"freebusy error for {calendar_id}: {cal['errors']}")
        return [
            (
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            )
            for b in cal["busy"]
        ]

    def insert(self, calendar_id, start, end, summary, description):
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": self.tz},
            "end": {"dateTime": end.isoformat(), "timeZone": self.tz},
        }
        return self.svc.events().insert(calendarId=calendar_id, body=body).execute()["id"]

    def overlapping(self, calendar_id, start, end):
        items = (
            self.svc.events()
            .list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
            )
            .execute()
            .get("items", [])
        )
        return [
            {"id": e["id"], "created": e.get("created", "")}
            for e in items
            if e.get("status") != "cancelled" and e.get("transparency") != "transparent"
        ]

    def delete(self, calendar_id, event_id):
        self.svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()


_gateway: CalendarGateway | None = None


def get_calendar() -> CalendarGateway:
    global _gateway
    if _gateway is None:
        s = get_settings()
        if not s.google_service_account_json:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
        _gateway = GoogleCalendarGateway(s.google_service_account_json, get_rules().timezone)
    return _gateway


def set_calendar(gw: CalendarGateway | None) -> None:
    global _gateway
    _gateway = gw


# ---------- website demo: no real calendars ----------
DEMO_DESIGNER_ID = "demo-designer"


class DemoCalendar:
    """Always free; 'bookings' are not stored anywhere. Used only by the website demo."""

    def busy(self, calendar_id, start, end):
        return []

    def insert(self, calendar_id, start, end, summary, description):
        return f"demo-{start:%Y%m%d%H%M}"

    def overlapping(self, calendar_id, start, end):
        return []

    def delete(self, calendar_id, event_id):
        return None


def demo_designers() -> list[Designer]:
    """One virtual designer covering every served locality, for the website demo."""
    rules = get_rules()
    return [
        Designer(
            id=DEMO_DESIGNER_ID,
            name="Aangan design team",
            telegram_chat_id="",
            calendar_id="demo",
            areas=list(rules.service_area.allow),
            handles_commercial=True,
        )
    ]


# ---------- slot selection ----------
@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    designer_id: str

    @property
    def label(self) -> str:
        return self.start.strftime("%A %d %B, %I:%M %p").replace(" 0", " ")


def eligible_designers(designers: list[Designer], locality: str | None, commercial: bool) -> list[Designer]:
    canonical = resolve_locality(locality).canonical
    out = []
    for d in designers:
        if not d.active:
            continue
        if commercial and not d.handles_commercial:
            continue
        if canonical and canonical.lower() not in {a.lower() for a in d.areas}:
            continue
        out.append(d)
    # Round robin: least recently assigned first; never-assigned first of all.
    return sorted(out, key=lambda d: d.last_assigned_at.timestamp() if d.last_assigned_at else 0)


def _candidate_starts(consult_type: str, now: datetime, rules: Rules) -> list[datetime]:
    b = rules.booking
    days = set(b.working_days) | (set(b.site_visit_extra_days) if consult_type == "site" else set())
    minutes = b.slot_minutes[consult_type]
    earliest = now + timedelta(hours=b.min_notice_hours)
    starts = []
    for offset in range(b.horizon_days + 1):
        day: date = (now + timedelta(days=offset)).date()
        if WEEKDAYS[day.weekday()] not in days:
            continue
        t = datetime.combine(day, rules.office_hours.start, tzinfo=now.tzinfo)
        close = datetime.combine(day, rules.office_hours.end, tzinfo=now.tzinfo)
        while t + timedelta(minutes=minutes) <= close:
            if t >= earliest:
                starts.append(t)
            t += timedelta(minutes=minutes)
    return starts


def _free(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return all(end <= b0 or start >= b1 for b0, b1 in busy)


def find_slots(
    gw: CalendarGateway,
    designers: list[Designer],
    consult_type: str,
    now: datetime,
    rules: Rules | None = None,
) -> list[Slot]:
    """2–3 slots on different days, from the least-recently-assigned designer first."""
    r = rules or get_rules()
    minutes = r.booking.slot_minutes[consult_type]
    starts = _candidate_starts(consult_type, now, r)
    if not starts:
        return []
    window_end = starts[-1] + timedelta(minutes=minutes)
    picked: list[Slot] = []
    for d in designers:
        try:
            busy = gw.busy(d.calendar_id, starts[0], window_end)
        except Exception:
            log.exception("freebusy failed for designer %s", d.id)
            continue
        used_days = {s.start.date() for s in picked}
        for st in starts:
            if len(picked) >= r.booking.slots_to_offer:
                return picked
            end = st + timedelta(minutes=minutes)
            if st.date() in used_days or not _free(st, end, busy):
                continue
            picked.append(Slot(st, end, d.id))
            used_days.add(st.date())
    return picked


# ---------- booking ----------
@dataclass(frozen=True)
class BookingResult:
    confirmed: bool
    booking_id: str | None = None
    reason: str | None = None


_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(calendar_id: str, start: datetime) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((calendar_id, start.isoformat()), threading.Lock())


def book(
    gw: CalendarGateway,
    designer: Designer,
    start: datetime,
    consult_type: str,
    summary: str,
    description: str,
    rules: Rules | None = None,
) -> BookingResult:
    """Never raises. confirmed=True only after Google returns the event and no
    earlier-created event overlaps it."""
    r = rules or get_rules()
    end = start + timedelta(minutes=r.booking.slot_minutes[consult_type])
    with _lock_for(designer.calendar_id, start):  # same-process race
        try:
            if not _free(start, end, gw.busy(designer.calendar_id, start, end)):
                return BookingResult(False, reason="slot_taken")
        except Exception:
            log.exception("free/busy re-check failed")
            return BookingResult(False, reason="calendar_unavailable")

        event_id = None
        for attempt in range(r.booking.insert_retries + 1):
            try:
                event_id = gw.insert(designer.calendar_id, start, end, summary, description)
                break
            except Exception:
                log.exception("calendar insert failed (attempt %d)", attempt + 1)
        if not event_id:
            return BookingResult(False, reason="calendar_insert_failed")

        # Cross-process race: if another event in this window was created before ours, back out.
        try:
            events = gw.overlapping(designer.calendar_id, start, end)
            ours = next((e for e in events if e["id"] == event_id), None)
            rivals = [e for e in events if e["id"] != event_id]
            if ours and any(e["created"] <= ours["created"] for e in rivals):
                gw.delete(designer.calendar_id, event_id)
                return BookingResult(False, reason="slot_taken")
        except Exception:
            log.exception("post-insert overlap check failed; keeping booking")
        return BookingResult(True, booking_id=event_id)

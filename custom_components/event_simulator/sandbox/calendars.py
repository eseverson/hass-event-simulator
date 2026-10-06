"""Calendar events for calendar triggers: the scenario's `calendars` block, and the events a calendar entity's recorded states name.

Standard library only: the sandbox child serves these events to Home Assistant's calendar trigger, and the live side (the miner, the panel's calendar lane) reads the same ones.

An event is `{start, end, summary, description?, location?}`, the shape `calendar.get_events` answers with: `start` / `end` are both dates (`2026-10-08`, an all-day event from that local midnight to the end's) or both datetimes (ISO 8601; one without an offset is local wall time in the instance's time zone).

A calendar entity's state names one event, its current or next one, in its attributes: `message` (the summary), `start_time` / `end_time` (local wall time, `%Y-%m-%d %H:%M:%S`), `all_day`, `location` (the recorder leaves `description` out, so it is never read from states: one there came from the live state). The distinct events its recorded states name are a lower bound on the calendar's events (two events at once show only one, and an event the entity never showed as next is missing), but each is known before it starts, so a trigger firing ahead of an event (a negative offset) finds it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

# Most events one calendar may carry in a scenario (a month of a busy calendar is a few hundred).
MAX_CALENDAR_EVENTS = 2000
# Most calendars one scenario may carry.
MAX_CALENDARS = 50
# The attributes' time format (`homeassistant.const` DATE_STR_FORMAT, which calendar state attributes use).
_STATE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True, slots=True)
class Event:
    """One calendar event: `start` / `end` both dates (all day) or both aware datetimes."""

    start: date | datetime
    end: date | datetime
    summary: str
    description: str | None = None
    location: str | None = None

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def start_local(self, tz: tzinfo) -> datetime:
        """When it starts, as an aware local datetime (an all-day event at its local midnight), as Home Assistant's `start_datetime_local`."""
        return _local(self.start, tz)

    def end_local(self, tz: tzinfo) -> datetime:
        return _local(self.end, tz)

    def as_dict(self) -> dict[str, Any]:
        """The scenario's shape (ISO strings)."""
        out: dict[str, Any] = {"start": self.start.isoformat(), "end": self.end.isoformat(), "summary": self.summary}
        if self.description:
            out["description"] = self.description
        if self.location:
            out["location"] = self.location
        return out


def _local(value: date | datetime, tz: tzinfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(tz)
    return datetime(value.year, value.month, value.day, tzinfo=tz)


def _parse_moment(value: Any, tz: tzinfo, what: str) -> date | datetime:
    if isinstance(value, datetime):
        parsed: date | datetime = value
    elif isinstance(value, date):
        return value
    elif isinstance(value, str) and value:
        try:
            parsed = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
        except ValueError as err:
            raise ValueError(f"{what} is not an ISO 8601 date or datetime: {value!r}") from err
    else:
        raise ValueError(f"{what} must be an ISO 8601 date or datetime string")
    if isinstance(parsed, datetime) and (parsed.tzinfo is None or parsed.utcoffset() is None):
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def parse_event(item: Any, tz: tzinfo, what: str = "event") -> Event:
    """One event of the scenario's shape; raises ValueError naming `what` for anything Home Assistant's calendar would refuse."""
    if not isinstance(item, Mapping):
        raise ValueError(f"{what} must be an object")
    start = _parse_moment(item.get("start"), tz, f"{what}.start")
    end = _parse_moment(item.get("end"), tz, f"{what}.end")
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ValueError(f"{what}: start and end must both be dates or both be datetimes")
    if not isinstance(start, datetime) and start == end:
        end = start + timedelta(days=1)  # as Home Assistant reads a one-day all-day event written with start == end
    if end <= start:
        raise ValueError(f"{what} must end after it starts")
    summary = item.get("summary")
    if not isinstance(summary, str):
        raise ValueError(f"{what}.summary must be a string")
    extra = {}
    for key in ("description", "location"):
        value = item.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{what}.{key} must be a string")
        extra[key] = value or None
    return Event(start, end, summary, **extra)


def parse_calendars(value: Any, tz: tzinfo) -> dict[str, list[Event]]:
    """The scenario's `calendars` (`{entity_id: [event, ...]}`), each calendar's events sorted by start; raises ValueError."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("calendars must be an object keyed by calendar entity_id")
    if len(value) > MAX_CALENDARS:
        raise ValueError(f"calendars carries {len(value)} calendars; the limit is {MAX_CALENDARS}")
    found: dict[str, list[Event]] = {}
    for entity_id, items in value.items():
        if not isinstance(entity_id, str) or not entity_id.startswith("calendar."):
            raise ValueError(f"calendars: {entity_id!r} is not a calendar entity_id")
        if not isinstance(items, list):
            raise ValueError(f"calendars[{entity_id!r}] must be a list of events")
        if len(items) > MAX_CALENDAR_EVENTS:
            raise ValueError(f"calendars[{entity_id!r}] carries {len(items)} events; the limit is {MAX_CALENDAR_EVENTS}")
        events = [parse_event(item, tz, f"calendars[{entity_id!r}][{n}]") for n, item in enumerate(items)]
        found[entity_id] = sort_events(events, tz)
    return found


def sort_events(events: Iterable[Event], tz: tzinfo) -> list[Event]:
    return sorted(events, key=lambda event: (event.start_local(tz), event.end_local(tz), event.summary))


def event_from_attributes(attributes: Mapping[str, Any], tz: tzinfo) -> Event | None:
    """The event a calendar entity's state attributes name, or None (no event, or attributes it cannot read)."""
    start_raw, end_raw = attributes.get("start_time"), attributes.get("end_time")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return None
    try:
        start = datetime.strptime(start_raw, _STATE_TIME_FORMAT)
        end = datetime.strptime(end_raw, _STATE_TIME_FORMAT)
    except ValueError:
        return None
    message = attributes.get("message")
    summary = message if isinstance(message, str) else ""
    location = attributes.get("location")
    extra = {"location": location if isinstance(location, str) and location else None}
    if attributes.get("all_day") is True:
        first, last = start.date(), end.date()
        if last == first:
            last = first + timedelta(days=1)
        return Event(first, last, summary, **extra) if last > first else None
    start_aware, end_aware = start.replace(tzinfo=tz), end.replace(tzinfo=tz)
    return Event(start_aware, end_aware, summary, **extra) if end_aware > start_aware else None


def events_from_states(attributes: Iterable[Mapping[str, Any]], tz: tzinfo) -> list[Event]:
    """The distinct events a calendar entity's recorded states name (their attribute dicts, any order), sorted by start: a lower bound on its events (see the module docstring)."""
    found: dict[tuple[Any, ...], Event] = {}
    for attrs in attributes:
        if (event := event_from_attributes(attrs, tz)) is not None:
            found.setdefault(_key(event, tz), event)
    return sort_events(found.values(), tz)


def _key(event: Event, tz: tzinfo) -> tuple[Any, ...]:
    return (event.start_local(tz).timestamp(), event.end_local(tz).timestamp(), event.summary)


def merge_events(primary: Iterable[Event], extra: Iterable[Event], tz: tzinfo) -> list[Event]:
    """`primary` (what the calendar answers now) plus the events of `extra` (what its recorded states named) it lacks: an event deleted since, or one the calendar no longer returns for the past. Same start, end and summary is the same event."""
    found: dict[tuple[Any, ...], Event] = {}
    for event in (*primary, *extra):
        found.setdefault(_key(event, tz), event)
    return sort_events(found.values(), tz)


def active(events: Iterable[Event], start: datetime, end: datetime, tz: tzinfo) -> list[int]:
    """Indexes of the events active in `[start, end)`, as a calendar's `async_get_events` answers: starting before `end`, ending at or after `start`.

    An event ending exactly at `start` counts, as Home Assistant's demo calendar counts it: the calendar trigger asks for the events of consecutive 15-minute spans and fires an end trigger in the span its end falls in, so an event ending on a span boundary must be in that span's answer. Live, the spans start wherever the trigger attached and rarely meet an event's end exactly; in the sandbox they start at the window start, often a round hour.
    """
    return [n for n, event in enumerate(events) if event.start_local(tz) < end and event.end_local(tz) >= start]

"""Local wall-clock handling.

Every restaurant carries an IANA zone name. Reservations are supplied as bare local
wall-clock strings (`YYYY-MM-DDTHH:MM`) and resolved against that zone, so this module
owns the two hard cases:

* **Spring forward** -- a local time inside the skipped hour does not exist. It is not
  bookable, and it never appears in availability.
* **Fall back** -- a local time inside the repeated hour exists twice. It always
  resolves to the *first* occurrence, the one before the clocks change.

Durations are absolute time, never wall-clock: a 90-minute booking does not become
150 minutes because a clock moved. Everything here therefore returns an instant
(timezone-aware, in UTC) and the display formatting happens on the way out.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc

# `starts_at_local` is a bare local time: no offset, no `Z`, no seconds.
LOCAL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
HHMM_RE = re.compile(r"^(\d{2}):(\d{2})$")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def zone(name):
    """The zone for an IANA name, or None when it is unknown."""
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return None


def parse_date(text):
    """A strict `YYYY-MM-DD` date, or None."""
    if not isinstance(text, str):
        return None
    match = DATE_RE.match(text)
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_hhmm(text):
    """A strict `HH:MM` clock time in minutes since midnight, or None."""
    if not isinstance(text, str):
        return None
    match = HHMM_RE.match(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def parse_local(text):
    """A strict bare-local ``YYYY-MM-DDTHH:MM`` as a naive datetime, or None.

    Seconds, offsets and a trailing `Z` are rejected here rather than normalised:
    the spec says `starts_at_local` is wall-clock with no offset and no `Z`, and a
    value that carries one is a validation failure rather than a thing to coerce.
    """
    if not isinstance(text, str):
        return None
    match = LOCAL_RE.match(text)
    if not match:
        return None
    try:
        return dt.datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                           int(match.group(4)), int(match.group(5)))
    except ValueError:
        return None


def resolve_local(naive: dt.datetime, tz) -> tuple:
    """``(instant, exists)`` for a naive wall-clock reading in `tz`.

    `instant` is UTC-aware. When the reading is repeated (fall back) the earliest
    instant wins, which is the first occurrence. When it is skipped (spring forward)
    `exists` is False and there is no instant.
    """
    fold0 = naive.replace(tzinfo=tz, fold=0)
    fold1 = naive.replace(tzinfo=tz, fold=1)
    off0, off1 = fold0.utcoffset(), fold1.utcoffset()
    if off0 == off1:
        # Unambiguous: the offset is the same whichever fold we ask for.
        return naive.replace(tzinfo=UTC) - off0, True
    best = None
    for offset in (off0, off1):
        candidate = naive.replace(tzinfo=UTC) - offset
        if candidate.astimezone(tz).replace(tzinfo=None) == naive:
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return None, False
    return best, True


def to_local(instant: dt.datetime, tz) -> dt.datetime:
    return instant.astimezone(tz)


def format_hhmm(minutes: int) -> str:
    """Minutes since local midnight as `HH:MM`."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_local(instant: dt.datetime, tz) -> str:
    """RFC 3339 with an explicit offset, in the restaurant's zone.

    Whole seconds: RFC 3339 allows a fractional part, but a caller comparing the
    string against the documented `YYYY-MM-DDTHH:MM:SS+HH:MM` shape should not have
    to allow for it.
    """
    return instant.astimezone(tz).isoformat(timespec="seconds")


def format_utc(instant: dt.datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="seconds")


def parse_instant(text):
    """An RFC 3339 instant with an explicit offset, or None."""
    if not isinstance(text, str):
        return None
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def local_date_of(instant: dt.datetime, tz) -> dt.date:
    return instant.astimezone(tz).date()

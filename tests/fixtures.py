"""Fixtures for the repository's own checks, written from the specification.

Dates are chosen around 2026's daylight-saving transitions, because that is where
local-time handling is easiest to get subtly wrong:
    Europe/Berlin      spring forward 2026-03-29, fall back 2026-10-25
    America/New_York   spring forward 2026-03-08, fall back 2026-11-01
"""
from __future__ import annotations

from datetime import date, timedelta

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
       "display_name": "Ada"}
GRACE = {"id": "u_grace", "email": "grace@example.com", "password": "hopper 1906",
         "display_name": "Grace"}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def booking_date(days: int = 7) -> str:
    """A date a week out: on every weekday's schedule, never near a cutoff."""
    return (date.today() + timedelta(days=days)).isoformat()


def restaurant(rid="r_anker", name="Zum Anker", timezone="Europe/Berlin",
               slot_minutes=30, duration=90, cutoff=120, opens="18:00",
               closes="23:00", tables=None, weekdays=WEEKDAYS, combinable=None,
               managers=None):
    return {
        "combinable": list(combinable or []),
        "manager_user_ids": list(managers or []),
        "id": rid, "name": name, "timezone": timezone, "slot_minutes": slot_minutes,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": [{"weekday": day, "opens": opens, "closes": closes}
                          for day in weekdays],
        "tables": tables if tables is not None else [
            {"id": "t_1", "label": "1", "capacity": 2},
            {"id": "t_2", "label": "2", "capacity": 4},
            {"id": "t_3", "label": "3", "capacity": 6},
        ],
    }


def fixture(restaurants=None, users=None, reservations=None):
    return {
        "users": list(users if users is not None else [ADA, GRACE]),
        "restaurants": list(restaurants if restaurants is not None else [restaurant()]),
        "reservations": list(reservations or []),
    }


def policy(effective_from, slot_minutes=30, duration=90, cutoff=120, opens="18:00",
           closes="23:00", capacities=None, tables=("t_1", "t_2", "t_3")):
    return {
        "effective_from": effective_from, "slot_minutes": slot_minutes,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": [{"weekday": day, "opens": opens, "closes": closes}
                          for day in WEEKDAYS],
        "capacities": dict(capacities) if capacities else dict(zip(tables, (2, 4, 6))),
    }

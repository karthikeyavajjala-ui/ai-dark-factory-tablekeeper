"""Service state: the model, the fixture loader, and export/import.

The state lives in one process and is replaced wholesale, so the store is a plain
object rather than a database. Everything it holds is JSON-serialisable, which is what
makes `/_test/export` a straight snapshot and `/_test/import` a validate-then-swap.

Ordering that the spec fixes is kept implicitly: fixture order is the order tables and
restaurants are listed in, and publication order is the order policies are appended in.
Python mappings preserve insertion order, so the internal shape uses mappings and the
JSON boundary converts to lists where a list is the natural spelling.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import threading
from copy import deepcopy

from . import profile
from . import timeutil as t
from .util import (EMAIL_RE, MAX_ID_LENGTH, REFERENCE_RE, WEEKDAYS, invalid, malformed,
                   new_reference, new_token)

TRACK = "tablekeeper"
FORMAT_VERSION = 1

_HASH_PREFIX = "scrypt"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """scrypt with a per-password salt. Plaintext storage is not permitted."""
    salt = os.urandom(16)
    value = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                           r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM)
    return f"{_HASH_PREFIX}${salt.hex()}${value.hex()}"


def verify_password(password, encoded) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != _HASH_PREFIX:
            return False
        value = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                               n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                               maxmem=_SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(value.hex(), expected)


def serialize_state(state: dict) -> dict:
    """The JSON boundary: mappings become lists, order preserved on both sides."""
    return {
        "users": list(state["users"].values()),
        "tokens": dict(state["tokens"]),
        "restaurants": list(state["restaurants"].values()),
        "reservations": list(state["reservations"].values()),
        "policies": {rid: [dict(p) for p in items]
                     for rid, items in state["policies"].items()},
        "series": list(state["series"].values()),
        "closures": {rid: [dict(c) for c in items]
                     for rid, items in state["closures"].items()},
        "plans": list(state["plans"].values()),
        "restaurant_revision": dict(state["restaurant_revision"]),
        "counters": dict(state["counters"]),
        "idempotency": list(state["idempotency"]),
    }


def empty_state() -> dict:
    return {
        "users": {}, "tokens": {}, "restaurants": {}, "reservations": {},
        "policies": {}, "series": {}, "closures": {}, "plans": {},
        "restaurant_revision": {}, "counters": {}, "idempotency": [],
    }


# ------------------------------------------------------------ fixture loading ---

def _fixture_restaurant(raw, where: str) -> dict:
    """Normalise and validate one restaurant (fixture shape, or a stored one)."""
    if not isinstance(raw, dict):
        raise invalid(f"{where} must be an object")
    rid = raw.get("id")
    if not isinstance(rid, str) or not rid or len(rid) > MAX_ID_LENGTH:
        raise invalid(f"{where}.id must be a string of 1..{MAX_ID_LENGTH} characters")
    name = raw.get("name")
    if not isinstance(name, str):
        raise invalid(f"{where}.name must be a string")
    zone_name = raw.get("timezone")
    if t.zone(zone_name) is None:
        raise invalid(f"{where}.timezone is not a known IANA zone")
    grid = raw.get("slot_minutes")
    if not isinstance(grid, int) or isinstance(grid, bool) or grid < 1:
        raise invalid(f"{where}.slot_minutes must be a positive integer")
    duration = raw.get("reservation_duration_minutes")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1:
        raise invalid(f"{where}.reservation_duration_minutes must be a positive integer")
    cutoff = raw.get("cancellation_cutoff_minutes")
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff < 0:
        raise invalid(f"{where}.cancellation_cutoff_minutes must be a non-negative "
                      f"integer")
    hours = validate_opening_hours(raw.get("opening_hours", []),
                                   f"{where}.opening_hours")
    tables = validate_tables(raw.get("tables", []), where)
    table_ids = [table["id"] for table in tables]
    combinable = []
    for pair in raw.get("combinable", []) or []:
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(member, str) for member in pair)):
            raise invalid(f"{where}.combinable entries are pairs of table ids")
        if pair[0] == pair[1]:
            raise invalid(f"{where}.combinable pairs two distinct tables")
        if any(member not in table_ids for member in pair):
            raise invalid(f"{where}.combinable names a table this restaurant does not "
                          f"have")
        combinable.append(list(pair))
    managers = raw.get("manager_user_ids", []) or []
    if not isinstance(managers, list) or not all(isinstance(m, str) for m in managers):
        raise invalid(f"{where}.manager_user_ids must be a list of user ids")
    return {
        "id": rid, "name": name, "timezone": zone_name,
        "slot_minutes": grid,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": hours,
        "tables": tables,
        "combinable": combinable,
        "manager_user_ids": list(managers),
    }


def validate_opening_hours(hours, where: str) -> list:
    if not isinstance(hours, list):
        raise invalid(f"{where} must be a list")
    seen, out = set(), []
    for entry in hours:
        if not isinstance(entry, dict):
            raise invalid(f"{where} entries must be objects")
        weekday = entry.get("weekday")
        if weekday not in WEEKDAYS:
            raise invalid(f"{where} weekday must be one of {' '.join(WEEKDAYS)}")
        opens, closes = entry.get("opens"), entry.get("closes")
        opens_min, closes_min = t.parse_hhmm(opens), t.parse_hhmm(closes)
        if opens_min is None or closes_min is None:
            raise invalid(f"{where} opens and closes must be local HH:MM")
        if closes_min <= opens_min:
            raise invalid(f"{where} closes must be later than opens on the same day")
        if weekday in seen:
            raise invalid(f"{where} repeats the weekday {weekday}")
        seen.add(weekday)
        out.append({"weekday": weekday, "opens": opens, "closes": closes})
    return out


def validate_tables(tables, where: str) -> list:
    if not isinstance(tables, list):
        raise invalid(f"{where}.tables must be a list")
    seen, out = set(), []
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise invalid(f"{where}.tables[{index}] must be an object")
        tid = table.get("id")
        if not isinstance(tid, str) or not tid or len(tid) > MAX_ID_LENGTH:
            raise invalid(f"{where}.tables[{index}].id must be a string of at most "
                          f"{MAX_ID_LENGTH} characters")
        if tid in seen:
            raise invalid(f"{where}.tables repeats the id {tid}")
        seen.add(tid)
        capacity = table.get("capacity")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise invalid(f"{where}.tables[{index}].capacity must be a positive integer")
        label = table.get("label", tid)
        if not isinstance(label, str):
            raise invalid(f"{where}.tables[{index}].label must be a string")
        out.append({"id": tid, "label": label, "capacity": capacity})
    return out


def build_fixture_state(fixture) -> dict:
    """Validate a reset fixture and turn it into a fresh state object."""
    if not isinstance(fixture, dict):
        raise malformed("the fixture must be a JSON object")
    raw_users = fixture.get("users", [])
    if not isinstance(raw_users, list):
        raise invalid("users must be a list")
    users, tokens = {}, {}
    seen_emails = set()
    for index, raw in enumerate(raw_users):
        where = f"users[{index}]"
        if not isinstance(raw, dict):
            raise invalid(f"{where} must be an object")
        uid = raw.get("id")
        if not isinstance(uid, str) or not uid or len(uid) > MAX_ID_LENGTH:
            raise invalid(f"{where}.id must be a string of 1..{MAX_ID_LENGTH} characters")
        email = raw.get("email")
        if not isinstance(email, str) or not EMAIL_RE.match(email):
            raise invalid(f"{where}.email must be of the form local@domain")
        if uid in users or email.lower() in seen_emails:
            raise invalid(f"{where} repeats an id or an email")
        seen_emails.add(email.lower())
        password = raw.get("password")
        if not isinstance(password, str):
            raise invalid(f"{where}.password must be a string")
        display = raw.get("display_name", email.split("@")[0])
        if display is not None and not isinstance(display, str):
            raise invalid(f"{where}.display_name must be a string")
        users[uid] = {"id": uid, "email": email, "display_name": display,
                      "password_hash": hash_password(password)}

    raw_restaurants = fixture.get("restaurants", [])
    if not isinstance(raw_restaurants, list):
        raise invalid("restaurants must be a list")
    restaurants = {}
    for index, raw in enumerate(raw_restaurants):
        restaurant = _fixture_restaurant(raw, f"restaurants[{index}]")
        if restaurant["id"] in restaurants:
            raise invalid("restaurants must have distinct ids")
        restaurants[restaurant["id"]] = restaurant

    state = empty_state()
    state["users"] = users
    state["restaurants"] = restaurants
    state["restaurant_revision"] = {rid: 0 for rid in restaurants}
    state["counters"] = {"reservation": 0, "series": 0, "plan": 0, "user": len(users)}

    raw_reservations = fixture.get("reservations", [])
    if not isinstance(raw_reservations, list):
        raise invalid("reservations must be a list")
    for index, raw in enumerate(raw_reservations):
        where = f"reservations[{index}]"
        if not isinstance(raw, dict):
            raise invalid(f"{where} must be an object")
        reference = raw.get("reference")
        if not isinstance(reference, str) or not REFERENCE_RE.match(reference):
            raise invalid(f"{where}.reference must be 6..12 characters of A-Z0-9")
        if reference in state["reservations"]:
            raise invalid(f"{where}.reference is a duplicate")
        restaurant = restaurants.get(raw.get("restaurant_id"))
        if restaurant is None:
            raise invalid(f"{where}.restaurant_id is not a restaurant in this fixture")
        starts_local = t.parse_local(raw.get("starts_at_local"))
        if starts_local is None:
            raise invalid(f"{where}.starts_at_local must be a bare local "
                          f"YYYY-MM-DDTHH:MM")
        zone = t.zone(restaurant["timezone"])
        start, exists = t.resolve_local(starts_local, zone)
        if not exists:
            raise invalid(f"{where}.starts_at_local does not exist in "
                          f"{restaurant['timezone']}")
        party = raw.get("party_size")
        if not isinstance(party, int) or isinstance(party, bool) or party < 1:
            raise invalid(f"{where}.party_size must be a positive integer")
        if raw.get("table_ids") is not None:
            table_ids = raw["table_ids"]
            if not isinstance(table_ids, list) or not table_ids:
                raise invalid(f"{where}.table_ids must be a non-empty list")
        else:
            table_ids = [raw.get("table_id")]
        if not all(isinstance(x, str) and x for x in table_ids):
            raise invalid(f"{where} must name its tables with strings")
        status = raw.get("status", "confirmed")
        if status not in ("confirmed", "cancelled"):
            raise invalid(f"{where}.status must be confirmed or cancelled")
        state["counters"]["reservation"] += 1
        reservation_id = raw.get("id") or f"res_{state['counters']['reservation']}"
        if not isinstance(reservation_id, str) or len(reservation_id) > MAX_ID_LENGTH:
            raise invalid(f"{where}.id must be a string of at most {MAX_ID_LENGTH} "
                          f"characters")
        terms = terms_for_date(state, restaurant["id"], start.date())
        reservation = {
            "reservation_id": reservation_id,
            "reference": reference,
            "user_id": raw.get("user_id"),
            "restaurant_id": restaurant["id"],
            "table_ids": list(table_ids),
            "party_size": party,
            "status": status,
            "starts_at": t.format_utc(start),
            "ends_at": t.format_utc(start + dt.timedelta(
                minutes=terms["reservation_duration_minutes"])),
            "starts_at_local": raw["starts_at_local"],
            "created_at": t.format_utc(t.now()),
            "revision": 1,
            "accepted_terms": terms if profile.STAGE >= 3 else None,
            "scheduled_date": raw["starts_at_local"][:10],
            "series_id": None,
            "series_index": None,
            "exception": False,
            "cancelled_at": None,
            "history": [],
        }
        if profile.STAGE >= 3:
            reservation["history"] = [history_entry(
                reservation, 1, "created",
                [{"field": field, "from": None, "to": value}
                 for field, value in creation_changes(reservation)])]
        state["reservations"][reference] = reservation
    return serialize_state(state)


# --------------------------------------------------------------- model helpers ---

def creation_changes(reservation) -> list:
    """A `created` entry names all three fields, each from null."""
    tables = reservation["table_ids"]
    if len(tables) == 1:
        return [("table_id", tables[0]),
                ("starts_at_local", reservation["starts_at_local"]),
                ("party_size", reservation["party_size"])]
    return [("table_ids", list(tables)),
            ("starts_at_local", reservation["starts_at_local"]),
            ("party_size", reservation["party_size"])]


def history_entry(reservation, revision, event, changes, plan_id=None, zone=None) -> dict:
    entry = {
        "seq": len(reservation["history"]) + 1,
        "at": t.format_local(t.now(), zone) if zone is not None else t.format_utc(t.now()),
        "event": event,
        "changes": changes,
        "revision": revision,
        "accepted_terms": deepcopy(reservation.get("accepted_terms")),
    }
    if plan_id is not None:
        entry["plan_id"] = plan_id
    return entry


def policy_zero(restaurant) -> dict:
    """Policy 0: the fixture's own rules, which apply before any published policy."""
    return {
        "policy_version": 0,
        "effective_from": None,
        "slot_minutes": restaurant["slot_minutes"],
        "reservation_duration_minutes": restaurant["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": restaurant["cancellation_cutoff_minutes"],
        "opening_hours": [dict(h) for h in restaurant["opening_hours"]],
        "capacities": {table["id"]: table["capacity"] for table in restaurant["tables"]},
    }


def select_policy(state, restaurant_id: str, local_date) -> dict:
    """The policy that governs a booking on `local_date`.

    Greatest `effective_from` not later than the date; ties go to the greatest
    `policy_version`. Policy 0 applies whenever no published policy is in range.
    """
    restaurant = state["restaurants"][restaurant_id]
    if profile.STAGE < 3:
        return policy_zero(restaurant)
    candidates = [
        policy for policy in state["policies"].get(restaurant_id, [])
        if t.parse_date(policy["effective_from"]) is not None
        and t.parse_date(policy["effective_from"]) <= local_date
    ]
    if not candidates:
        return policy_zero(restaurant)
    return max(candidates,
               key=lambda p: (t.parse_date(p["effective_from"]), p["policy_version"]))


def terms_for_date(state, restaurant_id: str, local_date) -> dict:
    """The accepted-terms snapshot: the whole selected policy but `effective_from`."""
    policy = select_policy(state, restaurant_id, local_date)
    return {
        "policy_version": policy["policy_version"],
        "slot_minutes": policy["slot_minutes"],
        "reservation_duration_minutes": policy["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": policy["cancellation_cutoff_minutes"],
        "opening_hours": [dict(h) for h in policy["opening_hours"]],
        "capacities": dict(policy["capacities"]),
    }


# ---------------------------------------------------------------------- store ---

class Store:
    """All service state, plus the lock that makes every write atomic."""

    def __init__(self):
        self.lock = threading.RLock()
        self.restore(empty_state())

    def restore(self, state: dict) -> None:
        """Adopt `state` wholesale. Callers have already validated it."""
        self.users = {user["id"]: user for user in state["users"]}
        self.emails = {user["email"].lower(): user["id"] for user in state["users"]}
        self.tokens = dict(state["tokens"])
        self.restaurants = {r["id"]: r for r in state["restaurants"]}
        self.reservations = {r["reference"]: r for r in state["reservations"]}
        self.policies = {rid: [dict(p) for p in items]
                         for rid, items in state["policies"].items()}
        self.series = {s["series_id"]: s for s in state["series"]}
        self.closures = {rid: [dict(c) for c in items]
                         for rid, items in state["closures"].items()}
        self.plans = {p["plan_id"]: p for p in state["plans"]}
        self.restaurant_revision = dict(state["restaurant_revision"])
        self.counters = dict(state["counters"])
        # Idempotency receipts are keyed by the four things identifying a request.
        self.idempotency = {}
        for record in state["idempotency"]:
            self.idempotency[record_key(record)] = record

    def state(self) -> dict:
        """The serialized shape, as a fresh object."""
        return serialize_state({
            "users": self.users, "tokens": self.tokens,
            "restaurants": self.restaurants, "reservations": self.reservations,
            "policies": self.policies, "series": self.series,
            "closures": self.closures, "plans": self.plans,
            "restaurant_revision": self.restaurant_revision,
            "counters": self.counters,
            "idempotency": list(self.idempotency.values()),
        })

    def export_payload(self) -> dict:
        return {"track": TRACK, "format_version": FORMAT_VERSION,
                "state": deepcopy(self.state())}

    def swap(self, state: dict) -> None:
        """Replace every byte of state at once. Callers hold the lock."""
        self.restore(state)

    # -- small helpers -----------------------------------------------------

    def next_counter(self, name: str) -> int:
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    def unique_reference(self) -> str:
        for _ in range(1000):
            candidate = new_reference()
            if candidate not in self.reservations:
                return candidate
        raise RuntimeError("reference space exhausted")

    def bump_restaurant_revision(self, rid: str) -> int:
        value = self.restaurant_revision.get(rid, 0) + 1
        self.restaurant_revision[rid] = value
        return value

    def reset(self, fixture) -> None:
        """Replace all state with the fixture. Synchronous: 204 means it is visible."""
        self.restore(build_fixture_state(fixture))


def record_key(record) -> tuple:
    return (record["user_id"], record["key"], record["method"], record["path"])


# ------------------------------------------------------------------ import side ---

def validate_import(payload) -> dict:
    """Check an exported payload and return the state it carries.

    Every problem is a 422 and the destination is untouched: the caller swaps state
    only after this returns. The format is opaque to callers, so this is the only
    thing between a malformed artifact and a corrupted service.
    """
    if not isinstance(payload, dict):
        raise invalid("an import must be a JSON object")
    if payload.get("track") != TRACK:
        raise invalid(f"track must be {TRACK!r}")
    if payload.get("format_version") != FORMAT_VERSION:
        raise invalid(f"format_version must be {FORMAT_VERSION}")
    state = payload.get("state")
    if not isinstance(state, dict):
        raise invalid("state must be an object")
    for key in ("users", "restaurants", "reservations"):
        if key not in state:
            raise invalid(f"state.{key} is missing")
        if not isinstance(state[key], list):
            raise invalid(f"state.{key} must be a list")

    users, emails = {}, {}
    for index, raw in enumerate(state["users"]):
        where = f"state.users[{index}]"
        if not isinstance(raw, dict):
            raise invalid(f"{where} must be an object")
        uid = raw.get("id")
        if not isinstance(uid, str) or not uid or len(uid) > MAX_ID_LENGTH:
            raise invalid(f"{where}.id is not a valid id")
        email = raw.get("email")
        if not isinstance(email, str) or not EMAIL_RE.match(email):
            raise invalid(f"{where}.email is not of the form local@domain")
        password_hash = raw.get("password_hash")
        if not isinstance(password_hash, str) or not password_hash.startswith(
                f"{_HASH_PREFIX}$"):
            raise invalid(f"{where}.password_hash is not a supported hash")
        if uid in users or email.lower() in emails:
            raise invalid(f"{where} repeats an id or an email")
        display = raw.get("display_name")
        if display is not None and not isinstance(display, str):
            raise invalid(f"{where}.display_name must be a string")
        users[uid] = {"id": uid, "email": email, "display_name": display,
                      "password_hash": password_hash}
        emails[email.lower()] = uid

    tokens = state.get("tokens", {})
    if not isinstance(tokens, dict):
        raise invalid("state.tokens must be an object")
    for token, uid in tokens.items():
        if not isinstance(token, str) or not token or uid not in users:
            raise invalid("state.tokens refers to an unknown account")

    raw_restaurants = state["restaurants"]
    restaurants = {}
    for index, raw in enumerate(raw_restaurants):
        restaurant = _fixture_restaurant(raw, f"state.restaurants[{index}]")
        if restaurant["id"] in restaurants:
            raise invalid("state.restaurants has duplicate ids")
        restaurants[restaurant["id"]] = restaurant

    out = empty_state()
    out["users"] = users
    out["tokens"] = dict(tokens)
    out["restaurants"] = restaurants
    out["restaurant_revision"] = {rid: 0 for rid in restaurants}
    out["counters"] = {"reservation": 0, "series": 0, "plan": 0, "user": len(users)}

    for index, raw in enumerate(state["reservations"]):
        reservation = _import_reservation(raw, index, restaurants)
        if reservation["reference"] in out["reservations"]:
            raise invalid("state.reservations repeats a reference")
        out["reservations"][reservation["reference"]] = reservation

    out["policies"] = _import_policies(state.get("policies", {}), restaurants)
    out["series"] = _import_series(state.get("series", []), out["reservations"])
    out["closures"] = _import_closures(state.get("closures", {}), restaurants)
    out["plans"] = _import_plans(state.get("plans", []), restaurants)
    out["idempotency"] = _import_idempotency(state.get("idempotency", []), users)

    revisions = state.get("restaurant_revision", {})
    if isinstance(revisions, dict) and profile.STAGE >= 3:
        for rid, value in revisions.items():
            if rid in restaurants and isinstance(value, int) and not isinstance(value, bool):
                out["restaurant_revision"][rid] = max(0, value)
    counters = state.get("counters", {})
    if isinstance(counters, dict):
        for name in ("reservation", "series", "plan", "user"):
            value = counters.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out["counters"][name] = max(out["counters"].get(name, 0), value)
    return serialize_state(out)


def _import_reservation(raw, index: int, restaurants: dict) -> dict:
    where = f"state.reservations[{index}]"
    if not isinstance(raw, dict):
        raise invalid(f"{where} must be an object")
    reference = raw.get("reference")
    if not isinstance(reference, str) or not REFERENCE_RE.match(reference):
        raise invalid(f"{where}.reference is not 6..12 characters of A-Z0-9")
    restaurant = restaurants.get(raw.get("restaurant_id"))
    if restaurant is None:
        raise invalid(f"{where}.restaurant_id is unknown")
    table_ids = raw.get("table_ids")
    if (not isinstance(table_ids, list) or not table_ids
            or not all(isinstance(x, str) and x for x in table_ids)):
        raise invalid(f"{where}.table_ids must be a non-empty list of strings")
    party = raw.get("party_size")
    if not isinstance(party, int) or isinstance(party, bool) or party < 1:
        raise invalid(f"{where}.party_size must be a positive integer")
    status = raw.get("status")
    if status not in ("confirmed", "cancelled"):
        raise invalid(f"{where}.status must be confirmed or cancelled")
    start = t.parse_instant(raw.get("starts_at"))
    end = t.parse_instant(raw.get("ends_at"))
    created = t.parse_instant(raw.get("created_at"))
    if start is None or end is None or created is None:
        raise invalid(f"{where} carries an unreadable timestamp")
    if end <= start:
        raise invalid(f"{where}.ends_at is not after starts_at")
    starts_local = t.parse_local(raw.get("starts_at_local"))
    if starts_local is None:
        raise invalid(f"{where}.starts_at_local is not a bare local YYYY-MM-DDTHH:MM")
    resolved, exists = t.resolve_local(starts_local, t.zone(restaurant["timezone"]))
    if not exists or resolved != start:
        raise invalid(f"{where}.starts_at_local does not match starts_at in "
                      f"{restaurant['timezone']}")
    revision = raw.get("revision", 1)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise invalid(f"{where}.revision must be a positive integer")
    history = raw.get("history", [])
    if not isinstance(history, list):
        raise invalid(f"{where}.history must be a list")
    terms = raw.get("accepted_terms")
    if terms is not None and not isinstance(terms, dict):
        raise invalid(f"{where}.accepted_terms must be an object")
    user_id = raw.get("user_id")
    if user_id is not None and not isinstance(user_id, str):
        raise invalid(f"{where}.user_id must be a string")
    reservation_id = raw.get("reservation_id") or f"res_{index + 1}"
    if not isinstance(reservation_id, str) or len(reservation_id) > MAX_ID_LENGTH:
        raise invalid(f"{where}.reservation_id is not a valid id")
    return {
        "reservation_id": reservation_id,
        "reference": reference,
        "user_id": user_id,
        "restaurant_id": restaurant["id"],
        "table_ids": list(table_ids),
        "party_size": party,
        "status": status,
        # Timestamps are echoed exactly as they were exported: an import must not
        # regenerate them, and the offset the restaurant's zone produced is part of
        # what a caller sees.
        "starts_at": raw["starts_at"],
        "ends_at": raw["ends_at"],
        "starts_at_local": raw["starts_at_local"],
        "created_at": raw["created_at"],
        "revision": revision,
        "accepted_terms": terms if profile.STAGE >= 3 else None,
        "scheduled_date": raw.get("scheduled_date") or raw["starts_at_local"][:10],
        "series_id": raw.get("series_id"),
        "series_index": raw.get("series_index"),
        "exception": bool(raw.get("exception", False)),
        "cancelled_at": raw.get("cancelled_at"),
        "history": history if profile.STAGE >= 3 else [],
    }


def _import_policies(raw, restaurants: dict) -> dict:
    if not isinstance(raw, dict):
        raise invalid("state.policies must be an object")
    out = {}
    for rid, items in raw.items():
        if rid not in restaurants or not isinstance(items, list):
            raise invalid("state.policies names an unknown restaurant")
        table_ids = {table["id"] for table in restaurants[rid]["tables"]}
        seen_versions, collected = set(), []
        for entry in items:
            if not isinstance(entry, dict):
                raise invalid("state.policies entries must be objects")
            version = entry.get("policy_version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise invalid("a stored policy_version must be a positive integer")
            if version in seen_versions:
                raise invalid("a stored policy_version is a duplicate")
            seen_versions.add(version)
            effective = entry.get("effective_from")
            if t.parse_date(effective) is None:
                raise invalid("a stored policy effective_from is not a date")
            capacities = entry.get("capacities")
            if not isinstance(capacities, dict) or set(capacities) != table_ids:
                raise invalid("a stored policy must name exactly the restaurant's tables")
            for field in ("slot_minutes", "reservation_duration_minutes",
                          "cancellation_cutoff_minutes"):
                value = entry.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise invalid(f"a stored policy {field} must be a non-negative "
                                  f"integer")
            if entry["slot_minutes"] < 1 or entry["reservation_duration_minutes"] < 1:
                raise invalid("a stored policy grid and duration must be positive")
            if any(not isinstance(v, int) or isinstance(v, bool) or v < 1
                   for v in capacities.values()):
                raise invalid("a stored policy capacity must be a positive integer")
            collected.append({
                "policy_version": version,
                "effective_from": effective,
                "slot_minutes": entry["slot_minutes"],
                "reservation_duration_minutes": entry["reservation_duration_minutes"],
                "cancellation_cutoff_minutes": entry["cancellation_cutoff_minutes"],
                "opening_hours": validate_opening_hours(
                    entry.get("opening_hours", []), "a stored policy opening_hours"),
                "capacities": dict(capacities),
            })
        out[rid] = collected
    return out


def _import_series(raw, reservations: dict) -> dict:
    if not isinstance(raw, list):
        raise invalid("state.series must be a list")
    out = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise invalid("state.series entries must be objects")
        sid = entry.get("series_id")
        if not isinstance(sid, str) or not sid or len(sid) > MAX_ID_LENGTH:
            raise invalid("a stored series_id is not a valid id")
        occurrences = entry.get("occurrences")
        if not isinstance(occurrences, list) or not occurrences:
            raise invalid("a stored series must carry its occurrences")
        revision = entry.get("revision", 1)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise invalid("a stored series revision must be a positive integer")
        if not isinstance(entry.get("interval_weeks"), int) or isinstance(
                entry["interval_weeks"], bool):
            raise invalid("a stored series interval_weeks must be an integer")
        seen_indices = set()
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                raise invalid("stored occurrences must be objects")
            index = occurrence.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise invalid("a stored occurrence index must be a non-negative integer")
            if index in seen_indices:
                raise invalid("a stored series repeats an occurrence index")
            seen_indices.add(index)
            if occurrence.get("reference") not in reservations:
                raise invalid("a stored series names an unknown occurrence")
            if t.parse_date(occurrence.get("scheduled_date")) is None:
                raise invalid("a stored occurrence has no scheduled date")
        out[sid] = {
            "series_id": sid,
            "user_id": entry.get("user_id"),
            "restaurant_id": entry.get("restaurant_id"),
            "interval_weeks": entry["interval_weeks"],
            "revision": revision,
            "created_at": entry.get("created_at"),
            "occurrences": [{"index": occurrence["index"],
                             "reference": occurrence["reference"],
                             "scheduled_date": occurrence["scheduled_date"]}
                            for occurrence in occurrences],
        }
    return out


def _import_closures(raw, restaurants: dict) -> dict:
    if not isinstance(raw, dict):
        raise invalid("state.closures must be an object")
    out = {}
    for rid, items in raw.items():
        if rid not in restaurants or not isinstance(items, list):
            raise invalid("state.closures names an unknown restaurant")
        table_ids = {table["id"] for table in restaurants[rid]["tables"]}
        collected = []
        for entry in items:
            if not isinstance(entry, dict) or entry.get("table_id") not in table_ids:
                raise invalid("a stored closure names an unknown table")
            start = t.parse_instant(entry.get("from"))
            end = t.parse_instant(entry.get("to"))
            if start is None or end is None or end <= start:
                raise invalid("a stored closure interval is invalid")
            collected.append({"table_id": entry["table_id"],
                              "from": t.format_utc(start), "to": t.format_utc(end)})
        out[rid] = collected
    return out


def _import_plans(raw, restaurants: dict) -> dict:
    if not isinstance(raw, list):
        raise invalid("state.plans must be a list")
    result = {}
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("plan_id"), str):
            raise invalid("a stored plan is malformed")
        if entry.get("restaurant_id") not in restaurants:
            raise invalid("a stored plan names an unknown restaurant")
        if not isinstance(entry.get("assignments"), list):
            raise invalid("a stored plan carries no assignments")
        result[entry["plan_id"]] = dict(entry)
    return result


def _import_idempotency(raw, users: dict) -> list:
    if not isinstance(raw, list):
        raise invalid("state.idempotency must be a list")
    result = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise invalid("state.idempotency entries must be objects")
        for field in ("user_id", "key", "method", "path"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise invalid("a stored idempotency receipt is malformed")
        if len(entry["key"]) > 255:
            raise invalid("a stored idempotency key is longer than 255 characters")
        if entry["user_id"] not in users:
            raise invalid("a stored idempotency receipt belongs to an unknown account")
        if "response" not in entry:
            raise invalid("a stored idempotency receipt has no response")
        result.append({"user_id": entry["user_id"], "key": entry["key"],
                       "method": entry["method"], "path": entry["path"],
                       "body": entry.get("body"), "status": entry.get("status", 201),
                       "response": entry["response"]})
    return result


# --------------------------------------------------------- token bookkeeping ---

def issue_token(store, user_id: str) -> str:
    token = new_token()
    store.tokens[token] = user_id
    return token


def user_for_token(store, token):
    return store.tokens.get(token)

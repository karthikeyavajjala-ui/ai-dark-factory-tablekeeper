"""The service's behaviour, with no HTTP in it.

Every public method returns ``(status, payload)`` or raises :class:`ApiError`. Every
public method holds the store's lock for its whole duration, which is what makes the
concurrency contract hold: two requests for the same table are decided one after the
other and the second sees the first one's occupancy. An idempotency receipt is written
before the lock is released, so a burst of identical requests produces exactly one 201
and the rest are replays.

Validation and commit are separated on purpose. A write computes everything it needs
-- the resulting fields, the policy that governs them, the occupancy it would produce
-- and only then mutates. That is what makes "a failed write changes nothing" true
without a rollback path, and it is what lets a batch validate every member before the
first one is committed.
"""
from __future__ import annotations

import datetime as dt

from . import profile
from . import store as store_mod
from . import timeutil as t
from .store import Store, history_entry, policy_zero, select_policy, terms_for_date
from .util import (ApiError, invalid, is_int, jsonable, malformed, not_found,
                   plain_decimal_digits, WEEKDAYS)

MAX_TABLE_SET = 2


# --------------------------------------------------------------------- helpers ---

def overlaps(a_start, a_end, b_start, b_end) -> bool:
    """Half-open interval intersection: `[start, end)` on both sides."""
    return a_start < b_end and b_start < a_end


def _duration_minutes(terms) -> int:
    return int(terms["reservation_duration_minutes"])


def _hours_for(terms, weekday: str):
    for entry in terms["opening_hours"]:
        if entry["weekday"] == weekday:
            return entry
    return None


def _slot_grid_of_day(terms, date) -> list:
    """Every grid step from `opens` where slot + duration <= `closes`, as minutes.

    Wall-clock arithmetic on purpose: the grid is defined in the restaurant's local
    clock even on the day the clock moves, which is what makes a repeated hour appear
    once and a skipped hour unreachable.
    """
    hours = _hours_for(terms, WEEKDAYS[date.weekday()])
    if hours is None:
        return []
    opens = t.parse_hhmm(hours["opens"])
    closes = t.parse_hhmm(hours["closes"])
    duration = _duration_minutes(terms)
    step = int(terms["slot_minutes"])
    out, cursor = [], opens
    while cursor + duration <= closes:
        out.append(cursor)
        cursor += step
    return out


def _live_slots(terms, zone, date) -> list:
    """`(starts_at_local, instant)` for each grid step that actually exists."""
    out = []
    for minutes in _slot_grid_of_day(terms, date):
        naive = dt.datetime.combine(date, dt.time(minutes // 60, minutes % 60))
        instant, exists = t.resolve_local(naive, zone)
        if not exists:
            continue                      # spring forward: this slot never happens
        out.append((f"{date.isoformat()}T{t.format_hhmm(minutes)}", instant))
    return out


def _declared_pair(restaurant, option) -> list | None:
    """The declared pair matching a requested set, or None.

    Unordered: a reversed pair names the same set, and the declared order is the one
    kept.
    """
    if profile.STAGE < 2:
        return None
    wanted = set(option)
    for pair in restaurant["combinable"]:
        if set(pair) == wanted:
            return list(pair)
    return None


class Engine:
    def __init__(self, store: Store):
        self.store = store

    # -------------------------------------------------------------- lookups ---

    def _restaurant(self, rid) -> dict:
        if not isinstance(rid, str):
            raise malformed("restaurant_id must be a string")
        restaurant = self.store.restaurants.get(rid)
        if restaurant is None:
            raise not_found("no such restaurant")
        return restaurant

    def _reservation(self, reference, user_id) -> dict:
        reservation = self.store.reservations.get(reference)
        if reservation is None or reservation["user_id"] != user_id:
            # One 404 for "no such reference" and "not yours": whether someone else's
            # booking exists is not this caller's business.
            raise not_found("no such reservation")
        return reservation

    def _policy_state(self) -> dict:
        """The slice of state policy selection needs."""
        return {"restaurants": self.store.restaurants,
                "policies": self.store.policies}

    def _busy(self, restaurant_id: str) -> dict:
        """Confirmed occupancy, and applied closures, per table."""
        restaurant = self.store.restaurants[restaurant_id]
        busy = {table["id"]: [] for table in restaurant["tables"]}
        for reservation in self.store.reservations.values():
            if (reservation["restaurant_id"] != restaurant_id
                    or reservation["status"] != "confirmed"):
                continue
            span = (t.parse_instant(reservation["starts_at"]),
                    t.parse_instant(reservation["ends_at"]))
            for table_id in reservation["table_ids"]:
                busy.setdefault(table_id, []).append(span)
        for closure in self.store.closures.get(restaurant_id, []):
            busy.setdefault(closure["table_id"], []).append(
                (t.parse_instant(closure["from"]), t.parse_instant(closure["to"])))
        return busy

    @staticmethod
    def _free(busy, table_id: str, start, end) -> bool:
        return not any(overlaps(start, end, a, b) for a, b in busy.get(table_id, []))

    @staticmethod
    def _span_of(reservation):
        return (t.parse_instant(reservation["starts_at"]),
                t.parse_instant(reservation["ends_at"]))

    # ---------------------------------------------------------------- views ---

    def _view(self, reservation) -> dict:
        tables = reservation["table_ids"]
        view = {
            "reservation_id": reservation["reservation_id"],
            "reference": reservation["reference"],
            "restaurant_id": reservation["restaurant_id"],
            "party_size": reservation["party_size"],
            "status": reservation["status"],
            "starts_at_local": reservation["starts_at_local"],
            "starts_at": reservation["starts_at"],
            "ends_at": reservation["ends_at"],
            "created_at": reservation["created_at"],
        }
        if profile.STAGE >= 2:
            view["table_ids"] = list(tables)
            if len(tables) == 1:
                view["table_id"] = tables[0]
        else:
            view["table_id"] = tables[0]
        if profile.STAGE >= 3:
            view["revision"] = reservation["revision"]
            view["accepted_terms"] = jsonable(reservation["accepted_terms"])
        return view

    def _restaurant_view(self, restaurant) -> dict:
        view = {
            "id": restaurant["id"],
            "name": restaurant["name"],
            "timezone": restaurant["timezone"],
            "slot_minutes": restaurant["slot_minutes"],
            "reservation_duration_minutes": restaurant["reservation_duration_minutes"],
            "cancellation_cutoff_minutes": restaurant["cancellation_cutoff_minutes"],
            "opening_hours": [dict(h) for h in restaurant["opening_hours"]],
            "tables": [dict(table) for table in restaurant["tables"]],
        }
        if profile.STAGE >= 2:
            view["combinable"] = [list(pair) for pair in restaurant["combinable"]]
        if profile.STAGE >= 3:
            view["manager_user_ids"] = list(restaurant["manager_user_ids"])
        return view

    # ----------------------------------------------------------------- auth ---

    def signup(self, body) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        email, password = body.get("email"), body.get("password")
        display = body.get("display_name")
        if email is not None and not isinstance(email, str):
            raise malformed("email must be a string")
        if password is not None and not isinstance(password, str):
            raise malformed("password must be a string")
        if display is not None and not isinstance(display, str):
            raise malformed("display_name must be a string")
        if email is None or password is None:
            raise invalid("email and password are required")
        if not store_mod.EMAIL_RE.match(email):
            raise invalid("email must be of the form local@domain")
        if len(password) < 8:
            raise invalid("password must be at least 8 characters")
        with self.store.lock:
            if email.lower() in self.store.emails:
                raise ApiError(409, "email_taken", "that email is already registered")
            user_id = f"u_{self.store.next_counter('user')}"
            while user_id in self.store.users:
                user_id = f"u_{self.store.next_counter('user')}"
            if display is None:
                display = email.split("@")[0]
            self.store.users[user_id] = {"id": user_id, "email": email,
                                         "display_name": display,
                                         "password_hash": store_mod.hash_password(
                                             password)}
            self.store.emails[email.lower()] = user_id
            token = store_mod.issue_token(self.store, user_id)
        return 201, {"user_id": user_id, "display_name": display, "token": token}

    def login(self, body) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        email, password = body.get("email"), body.get("password")
        for field, value in (("email", email), ("password", password)):
            if value is not None and not isinstance(value, str):
                raise malformed(f"{field} must be a string")
        with self.store.lock:
            user_id = self.store.emails.get(email.lower()) if isinstance(email, str) \
                else None
            user = self.store.users.get(user_id) if user_id else None
            if user is None or not store_mod.verify_password(password,
                                                             user["password_hash"]):
                raise ApiError(401, "unauthenticated",
                               "wrong password or unknown email")
            token = store_mod.issue_token(self.store, user_id)
        return 200, {"user_id": user_id, "display_name": user["display_name"],
                     "token": token}

    def user_for_token(self, token):
        """The account behind a bearer token, or None."""
        with self.store.lock:
            user_id = self.store.tokens.get(token)
            return self.store.users.get(user_id) if user_id else None

    # ---------------------------------------------------------- restaurants ---

    def list_restaurants(self) -> tuple:
        with self.store.lock:
            return 200, {"restaurants": [
                {"id": r["id"], "name": r["name"], "timezone": r["timezone"]}
                for r in self.store.restaurants.values()]}

    def restaurant_detail(self, rid) -> tuple:
        with self.store.lock:
            return 200, self._restaurant_view(self._restaurant(rid))

    # --------------------------------------------------------- availability ---

    def availability(self, query: dict) -> tuple:
        for name in ("restaurant_id", "date", "party_size"):
            if query.get(name) is None or query.get(name) == "":
                raise invalid(f"{name} is required")
        party = plain_decimal_digits(query["party_size"])
        if party is None or party < 1:
            raise invalid("party_size must be plain decimal digits and at least 1")
        date = t.parse_date(query["date"])
        if date is None:
            raise invalid("date must be a local calendar date, YYYY-MM-DD")
        explain = False
        if profile.STAGE >= 3:
            raw = query.get("explain")
            if raw is not None and raw != "true":
                raise invalid("explain accepts only the value true")
            explain = raw == "true"
        with self.store.lock:
            restaurant = self._restaurant(query["restaurant_id"])
            terms = select_policy(self._policy_state(), restaurant["id"], date)
            zone = t.zone(restaurant["timezone"])
            busy = self._busy(restaurant["id"])
            slots = []
            for starts_local, start in _live_slots(terms, zone, date):
                end = start + dt.timedelta(minutes=_duration_minutes(terms))
                slot = {"starts_at_local": starts_local,
                        "starts_at": t.format_local(start, zone),
                        "available_table_ids": []}
                explanations = []
                for table in restaurant["tables"]:
                    capacity_ok = party <= terms["capacities"][table["id"]]
                    overlap_ok = self._free(busy, table["id"], start, end)
                    if capacity_ok and overlap_ok:
                        slot["available_table_ids"].append(table["id"])
                    if explain:
                        explanations.append({
                            "table_id": table["id"],
                            "policy_version": terms["policy_version"],
                            "available": capacity_ok and overlap_ok,
                            "rules": [{"rule": "capacity", "holds": capacity_ok},
                                      {"rule": "no_overlap", "holds": overlap_ok}],
                        })
                if profile.STAGE >= 2:
                    slot["available_options"] = self._available_options(
                        restaurant, terms, busy, party, start, end)
                if explain:
                    slot["explain"] = explanations
                slots.append(slot)
        return 200, {"restaurant_id": restaurant["id"], "date": date.isoformat(),
                     "timezone": restaurant["timezone"], "slots": slots}

    def _available_options(self, restaurant, terms, busy, party, start, end) -> list:
        """Singles first in fixture order, then declared pairs in declaration order."""
        out = []
        for table in restaurant["tables"]:
            if (party <= terms["capacities"][table["id"]]
                    and self._free(busy, table["id"], start, end)):
                out.append({"table_ids": [table["id"]],
                            "capacity": terms["capacities"][table["id"]]})
        for pair in restaurant["combinable"]:
            capacity = sum(terms["capacities"].get(member, 0) for member in pair)
            if capacity >= party and all(self._free(busy, member, start, end)
                                         for member in pair):
                out.append({"table_ids": list(pair), "capacity": capacity})
        return out

    # --------------------------------------------------------- reservations ---

    def _table_set(self, body, restaurant) -> list:
        """The requested table set, validated against this restaurant."""
        has_one = body.get("table_id") is not None
        has_many = body.get("table_ids") is not None
        known = {table["id"] for table in restaurant["tables"]}
        if profile.STAGE >= 2:
            if has_one and has_many:
                raise invalid("send table_id or table_ids, not both")
            if has_many:
                raw = body["table_ids"]
                if not isinstance(raw, list):
                    raise malformed("table_ids must be a list")
                if not raw:
                    raise invalid("table_ids must name at least one table")
                if not all(isinstance(x, str) for x in raw):
                    raise malformed("table_ids must be a list of table ids")
                if len(set(raw)) != len(raw):
                    raise invalid("table_ids repeats a table")
                if len(raw) > MAX_TABLE_SET:
                    raise ApiError(422, "combination_not_allowed",
                                   "a booking may hold at most two tables")
                if len(raw) == 2:
                    pair = _declared_pair(restaurant, raw)
                    if pair is None:
                        raise ApiError(422, "combination_not_allowed",
                                       "those tables are not declared combinable")
                    raw = pair
                tables = list(raw)
            elif has_one:
                if not isinstance(body["table_id"], str):
                    raise malformed("table_id must be a string")
                tables = [body["table_id"]]
            else:
                raise invalid("table_id or table_ids is required")
        else:
            if not has_one:
                raise invalid("table_id is required")
            if not isinstance(body["table_id"], str):
                raise malformed("table_id must be a string")
            tables = [body["table_id"]]
        for table_id in tables:
            if table_id not in known:
                # Unknown here, or a real table belonging to another restaurant.
                raise not_found("no such table at this restaurant")
        return tables

    def _resolve_start(self, restaurant, starts_at_local):
        """Resolve a wall-clock start, and the policy governing its date."""
        naive = t.parse_local(starts_at_local)
        if naive is None:
            raise invalid("starts_at_local must be a bare local YYYY-MM-DDTHH:MM")
        zone = t.zone(restaurant["timezone"])
        instant, exists = t.resolve_local(naive, zone)
        if not exists:
            raise ApiError(422, "invalid_local_time",
                           "that local time does not exist in this zone")
        terms = terms_for_date(self._policy_state(), restaurant["id"], naive.date())
        return naive, instant, terms, zone

    def _check_slot(self, terms, zone, naive) -> None:
        """Grid membership, opening hours, and the closing edge."""
        date = naive.date()
        minutes = naive.hour * 60 + naive.minute
        hours = _hours_for(terms, WEEKDAYS[date.weekday()])
        if hours is not None:
            opens = t.parse_hhmm(hours["opens"])
            if (minutes - opens) % int(terms["slot_minutes"]) != 0:
                raise ApiError(422, "not_on_slot_grid",
                               "that start time is not on the restaurant's grid")
        wanted = f"{date.isoformat()}T{t.format_hhmm(minutes)}"
        if wanted not in [label for label, _ in _live_slots(terms, zone, date)]:
            raise ApiError(422, "outside_opening_hours",
                           "the restaurant is not open for that booking")

    @staticmethod
    def _party_from(body) -> int:
        if body.get("party_size") is None:
            raise invalid("party_size is required")
        party = body["party_size"]
        # Endpoint-specific rule: every invalid value of this field is 422 whatever
        # its JSON type, strings and booleans included.
        if not is_int(party) or party < 1:
            raise invalid("party_size must be an integer of at least 1")
        return party

    @staticmethod
    def _check_capacity(terms, tables, party) -> None:
        total = sum(terms["capacities"].get(table_id, 0) for table_id in tables)
        if party > total:
            raise ApiError(422, "party_exceeds_capacity",
                           "that party does not fit the selected table set")

    def _new_reservation(self, user_id, restaurant, tables, party, naive, instant,
                         terms, series=None, bump=True) -> dict:
        end = instant + dt.timedelta(minutes=_duration_minutes(terms))
        zone = t.zone(restaurant["timezone"])
        self.store.next_counter("reservation")
        reference = self.store.unique_reference()
        reservation = {
            "reservation_id": f"res_{self.store.counters['reservation']}",
            "reference": reference,
            "user_id": user_id,
            "restaurant_id": restaurant["id"],
            "table_ids": list(tables),
            "party_size": party,
            "status": "confirmed",
            # Response timestamps are RFC 3339 with an explicit offset, in the
            # restaurant's own zone -- the same zone starts_at_local is wall-clock in.
            "starts_at": t.format_local(instant, zone),
            "ends_at": t.format_local(end, zone),
            "starts_at_local": f"{naive.date().isoformat()}T"
                               f"{t.format_hhmm(naive.hour * 60 + naive.minute)}",
            "created_at": t.format_utc(t.now()),
            "revision": 1,
            "accepted_terms": dict(terms) if profile.STAGE >= 3 else None,
            "scheduled_date": naive.date().isoformat(),
            "series_id": series[0] if series else None,
            "series_index": series[1] if series else None,
            "exception": False,
            "cancelled_at": None,
            "history": [],
        }
        if profile.STAGE >= 3:
            reservation["history"] = [history_entry(
                reservation, 1, "created",
                [{"field": field, "from": None, "to": value}
                 for field, value in store_mod.creation_changes(reservation)], zone=zone)]
        self.store.reservations[reference] = reservation
        if profile.STAGE >= 3 and bump:
            self.store.bump_restaurant_revision(restaurant["id"])
        return reservation

    # ---------------------------------------------------------- idempotency ---

    def _idempotent(self, user_id, key, method, path, body, run) -> tuple:
        """Resolve the key, run the operation, then store the receipt.

        Resolution happens after the body has been parsed as a JSON object and the
        caller authenticated, and before any endpoint-specific validation, so a used
        key with a different body is refused even when the new body is itself invalid.
        """
        if key is None or key == "":
            raise ApiError(400, "missing_idempotency_key",
                           "an Idempotency-Key header is required")
        if len(key) > 255:
            raise invalid("Idempotency-Key must be 1..255 characters")
        with self.store.lock:
            record = self.store.idempotency.get((user_id, key, method, path))
            if record is not None:
                if record["body"] == body:
                    return 200, jsonable(record["response"])
                raise ApiError(409, "idempotency_key_reuse",
                               "that key was already used with a different body")
            status, payload = run()
            if 200 <= status < 300:
                self.store.idempotency[(user_id, key, method, path)] = {
                    "user_id": user_id, "key": key, "method": method, "path": path,
                    "body": jsonable(body), "status": status,
                    "response": jsonable(payload),
                }
            return status, payload

    # ------------------------------------------------------------ bookings ---

    def create_reservation(self, body, user_id, key) -> tuple:
        """`POST /reservations`. Idempotent on `key`, scoped to the caller."""
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        return self._idempotent(user_id, key, "POST", "/reservations", body,
                                lambda: self._create_locked(body, user_id))

    def _create_locked(self, body, user_id) -> tuple:
        if body.get("restaurant_id") is None:
            raise invalid("restaurant_id is required")
        if not isinstance(body["restaurant_id"], str):
            raise malformed("restaurant_id must be a string")
        if body.get("starts_at_local") is None:
            raise invalid("starts_at_local is required")
        if not isinstance(body["starts_at_local"], str):
            raise malformed("starts_at_local must be a string")
        with self.store.lock:
            restaurant = self._restaurant(body["restaurant_id"])
            tables = self._table_set(body, restaurant)
            party = self._party_from(body)
            naive, instant, terms, zone = self._resolve_start(
                restaurant, body["starts_at_local"])
            self._check_slot(terms, zone, naive)
            self._check_capacity(terms, tables, party)
            end = instant + dt.timedelta(minutes=_duration_minutes(terms))
            busy = self._busy(restaurant["id"])
            if any(not self._free(busy, table_id, instant, end) for table_id in tables):
                raise ApiError(409, "table_unavailable",
                               "that table is taken for an overlapping interval")
            reservation = self._new_reservation(user_id, restaurant, tables, party,
                                                naive, instant, terms)
            return 201, self._view(reservation)

    def list_reservations(self, user_id) -> tuple:
        with self.store.lock:
            mine = [r for r in self.store.reservations.values()
                    if r["user_id"] == user_id]
            mine.sort(key=lambda r: (r["starts_at"], r["created_at"]), reverse=True)
            return 200, {"reservations": [self._view(r) for r in mine]}

    def get_reservation(self, reference, user_id) -> tuple:
        with self.store.lock:
            return 200, self._view(self._reservation(reference, user_id))

    def _accepted_cutoff(self, reservation) -> int:
        terms = reservation.get("accepted_terms")
        if profile.STAGE >= 3 and terms:
            return int(terms["cancellation_cutoff_minutes"])
        restaurant = self.store.restaurants[reservation["restaurant_id"]]
        return int(restaurant["cancellation_cutoff_minutes"])

    def _check_cutoff(self, reservation) -> None:
        start = t.parse_instant(reservation["starts_at"])
        cutoff = dt.timedelta(minutes=self._accepted_cutoff(reservation))
        if t.now() >= start - cutoff:
            raise ApiError(409, "cutoff_passed",
                           "that booking can no longer be changed or cancelled")

    def cancel_reservation(self, reference, user_id) -> tuple:
        with self.store.lock:
            reservation = self._reservation(reference, user_id)
            if reservation["status"] == "cancelled":
                return 200, self._view(reservation)
            self._check_cutoff(reservation)
            reservation["status"] = "cancelled"
            reservation["cancelled_at"] = t.format_utc(t.now())
            if profile.STAGE >= 3:
                zone = t.zone(self.store.restaurants[
                    reservation["restaurant_id"]]["timezone"])
                reservation["revision"] += 1
                reservation["history"].append(history_entry(
                    reservation, reservation["revision"], "cancelled", [], zone=zone))
                self.store.bump_restaurant_revision(reservation["restaurant_id"])
                self._touch_series(reservation, exception=False)
            return 200, self._view(reservation)

    def _touch_series(self, reservation, exception: bool, bump: bool = True) -> None:
        """Mark a member of an agreement, and move the agreement on.

        A batch reaches several occurrences at once: it marks each of them, then moves
        the series revision once for the whole operation, so `bump=False` here and a
        single bump by the caller.
        """
        series_id = reservation.get("series_id")
        if not series_id or series_id not in self.store.series:
            return
        if exception:
            reservation["exception"] = True
        if bump:
            self.store.series[series_id]["revision"] += 1

    @staticmethod
    def _check_expected_revision(reservation, body) -> None:
        raw = body.get("expected_revision")
        if raw is None:
            return
        if not is_int(raw) or raw < 1:
            raise invalid("expected_revision must be a positive integer")
        if raw != reservation["revision"]:
            raise ApiError(409, "stale_revision",
                           "that booking has moved on since you read it")

    def _resulting_fields(self, reservation, item: dict) -> dict:
        """The fields a PATCH or a move would leave behind.

        Fields the caller omitted keep their current values; unknown fields are
        ignored.
        """
        restaurant = self.store.restaurants[reservation["restaurant_id"]]
        result = {"table_ids": list(reservation["table_ids"]),
                  "party_size": reservation["party_size"],
                  "starts_at_local": reservation["starts_at_local"]}
        touches_tables = item.get("table_id") is not None or (
            profile.STAGE >= 2 and item.get("table_ids") is not None)
        if touches_tables:
            probe = {}
            if item.get("table_id") is not None:
                probe["table_id"] = item["table_id"]
            if profile.STAGE >= 2 and item.get("table_ids") is not None:
                probe["table_ids"] = item["table_ids"]
            result["table_ids"] = self._table_set(probe, restaurant)
        if item.get("party_size") is not None:
            result["party_size"] = self._party_from({"party_size": item["party_size"]})
        if item.get("starts_at_local") is not None:
            if not isinstance(item["starts_at_local"], str):
                raise malformed("starts_at_local must be a string")
            result["starts_at_local"] = item["starts_at_local"]
        return result

    def _plan_amendment(self, reservation, item: dict) -> tuple:
        """Validate a whole amendment without touching anything."""
        restaurant = self.store.restaurants[reservation["restaurant_id"]]
        wanted = self._resulting_fields(reservation, item)
        naive, instant, terms, zone = self._resolve_start(restaurant,
                                                          wanted["starts_at_local"])
        self._check_slot(terms, zone, naive)
        self._check_capacity(terms, wanted["table_ids"], wanted["party_size"])
        return wanted, naive, instant, terms

    @staticmethod
    def _history_changes(reservation, wanted) -> list:
        """Only the fields that actually changed, in table / start / party order."""
        old_tables, new_tables = reservation["table_ids"], wanted["table_ids"]
        changes = []
        if set(old_tables) != set(new_tables):
            if len(old_tables) == 1 and len(new_tables) == 1:
                changes.append({"field": "table_id", "from": old_tables[0],
                                "to": new_tables[0]})
            else:
                changes.append({"field": "table_ids", "from": list(old_tables),
                                "to": list(new_tables)})
        if wanted["starts_at_local"] != reservation["starts_at_local"]:
            changes.append({"field": "starts_at_local",
                            "from": reservation["starts_at_local"],
                            "to": wanted["starts_at_local"]})
        if wanted["party_size"] != reservation["party_size"]:
            changes.append({"field": "party_size", "from": reservation["party_size"],
                            "to": wanted["party_size"]})
        return changes

    def _commit_amendment(self, reservation, wanted, instant, terms,
                          series_effect: str = "full") -> bool:
        """Write a validated amendment. Returns False when nothing really changed.

        `series_effect` says what this amendment does to the agreement the booking may
        belong to: an individual PATCH (`full`) marks an exception and moves the series
        revision on; a batch marks its members (`exception_only`) and moves each affected
        series once itself; a series amendment changes no exception flags (`none`).
        """
        tables_moved = set(wanted["table_ids"]) != set(reservation["table_ids"])
        start_moved = instant != t.parse_instant(reservation["starts_at"])
        party_moved = wanted["party_size"] != reservation["party_size"]
        if not (tables_moved or start_moved or party_moved):
            # A no-op succeeds and changes nothing at all: no history, no revision,
            # no accepted terms, no end time.
            return False
        changes = self._history_changes(reservation, wanted)
        zone = t.zone(self.store.restaurants[reservation["restaurant_id"]]["timezone"])
        reservation["table_ids"] = list(wanted["table_ids"])
        reservation["party_size"] = wanted["party_size"]
        reservation["starts_at_local"] = wanted["starts_at_local"]
        reservation["starts_at"] = t.format_local(instant, zone)
        reservation["ends_at"] = t.format_local(
            instant + dt.timedelta(minutes=_duration_minutes(terms)), zone)
        if profile.STAGE >= 3:
            reservation["accepted_terms"] = dict(terms)
            reservation["revision"] += 1
            reservation["history"].append(history_entry(
                reservation, reservation["revision"], "changed", changes, zone=zone))
            self.store.bump_restaurant_revision(reservation["restaurant_id"])
            self._touch_series(reservation, exception=series_effect != "none",
                               bump=series_effect == "full")
        return True

    def patch_reservation(self, reference, body, user_id) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        with self.store.lock:
            reservation = self._reservation(reference, user_id)
            if reservation["status"] == "cancelled":
                raise ApiError(409, "reservation_cancelled",
                               "that booking is cancelled")
            if profile.STAGE >= 3:
                self._check_expected_revision(reservation, body)
            self._check_cutoff(reservation)
            wanted, _, instant, terms = self._plan_amendment(reservation, body)
            busy = self._busy(reservation["restaurant_id"])
            mine = self._span_of(reservation)
            for table_id in reservation["table_ids"]:
                busy[table_id] = [span for span in busy.get(table_id, []) if span != mine]
            end = instant + dt.timedelta(minutes=_duration_minutes(terms))
            if any(not self._free(busy, table_id, instant, end)
                   for table_id in wanted["table_ids"]):
                raise ApiError(409, "table_unavailable",
                               "that table is taken for an overlapping interval")
            self._commit_amendment(reservation, wanted, instant, terms)
            return 200, self._view(reservation)

    # ------------------------------------------------------------- history ---

    def reservation_history(self, reference, user_id) -> tuple:
        """Owner-only, and 404 even without a token: history never leaks."""
        with self.store.lock:
            reservation = self._reservation(reference, user_id)
            if profile.STAGE < 3:
                raise not_found("no such reservation")
            return 200, {"reference": reference,
                         "entries": [jsonable(entry)
                                     for entry in reservation["history"]]}

    def reservation_decision(self, reference, user_id) -> tuple:
        with self.store.lock:
            reservation = self._reservation(reference, user_id)
            if profile.STAGE < 3:
                raise not_found("no such reservation")
            return 200, {"reference": reference,
                         "revision": reservation["revision"],
                         "accepted_terms": jsonable(reservation["accepted_terms"])}

    # -------------------------------------------------------- atomic batch ---

    def reservation_moves(self, body, user_id, key) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        return self._idempotent(user_id, key, "POST", "/reservation-moves", body,
                                lambda: self._moves_locked(body, user_id))

    def _moves_locked(self, body, user_id) -> tuple:
        moves = body.get("moves")
        if moves is None:
            raise invalid("moves is required")
        if not isinstance(moves, list):
            raise malformed("moves must be a list")
        if not moves or len(moves) > 8:
            raise invalid("moves must hold 1..8 objects")
        references = []
        for item in moves:
            if not isinstance(item, dict):
                raise invalid("every move must be an object")
            reference = item.get("reference")
            if not isinstance(reference, str) or not reference:
                raise invalid("every move needs a string reference")
            references.append(reference)
        if len(set(references)) != len(references):
            raise invalid("moves must have distinct references")

        with self.store.lock:
            bookings = []
            for item in moves:
                reservation = self.store.reservations.get(item["reference"])
                if reservation is None or reservation["user_id"] != user_id:
                    raise not_found("no such reservation")
                bookings.append((item, reservation))
            restaurant_id = bookings[0][1]["restaurant_id"]
            if any(reservation["restaurant_id"] != restaurant_id
                   for _, reservation in bookings):
                raise invalid("every move must be for the same restaurant")
            restaurant = self.store.restaurants[restaurant_id]

            # Non-occupancy errors first, in input order, cutoff ahead of everything
            # else for that booking.
            staged = []
            for item, reservation in bookings:
                if profile.STAGE >= 3:
                    self._check_expected_revision(reservation, item)
                if reservation["status"] == "cancelled":
                    raise ApiError(409, "reservation_cancelled",
                                   "that booking is cancelled")
                self._check_cutoff(reservation)
                touched = {name: item[name] for name in
                           ("table_id", "table_ids", "starts_at_local", "party_size")
                           if name in item}
                wanted, _, instant, terms = self._plan_amendment(reservation, touched)
                staged.append((reservation, wanted, instant, terms))

            # Resulting occupancy. Every listed booking leaves its old slot before the
            # batch's new occupancy is examined, so listed bookings may swap with each
            # other and an unchanged listed booking keeps its own tables.
            listed = {reservation["reference"] for reservation, *_ in staged}
            outside = {}
            for table_id, spans in self._busy(restaurant_id).items():
                kept = []
                for span in spans:
                    holder = self._span_holder(restaurant_id, table_id, span)
                    if holder is not None and holder in listed:
                        continue
                    kept.append(span)
                outside[table_id] = kept
            results = []
            for index, (reservation, wanted, instant, terms) in enumerate(staged):
                end = instant + dt.timedelta(minutes=_duration_minutes(terms))
                for table_id in wanted["table_ids"]:
                    if not self._free(outside, table_id, instant, end):
                        raise ApiError(409, "table_unavailable",
                                       "that table is taken for an overlapping "
                                       "interval")
                for other, other_wanted, other_instant, other_terms in staged[index + 1:]:
                    if not set(wanted["table_ids"]) & set(other_wanted["table_ids"]):
                        continue
                    other_end = other_instant + dt.timedelta(
                        minutes=_duration_minutes(other_terms))
                    if overlaps(instant, end, other_instant, other_end):
                        raise ApiError(409, "table_unavailable",
                                       "two moves would fight over one table")
                results.append((reservation, wanted, instant, terms))

            views = []
            touched = []
            for reservation, wanted, instant, terms in results:
                if self._commit_amendment(reservation, wanted, instant, terms,
                                          series_effect="exception_only"):
                    series_id = reservation.get("series_id")
                    if series_id and series_id in self.store.series:
                        touched.append(series_id)
                views.append(self._view(reservation))
            # One bump per affected agreement for the whole batch, not one per move.
            for series_id in dict.fromkeys(touched):
                self.store.series[series_id]["revision"] += 1
            if profile.STAGE >= 3:
                self.store.bump_restaurant_revision(restaurant_id)
            return 201, {"reservations": views}

    def _span_holder(self, restaurant_id, table_id, span):
        """The reference of the confirmed booking occupying `span`, if any."""
        for reference, reservation in self.store.reservations.items():
            if (reservation["restaurant_id"] == restaurant_id
                    and reservation["status"] == "confirmed"
                    and table_id in reservation["table_ids"]
                    and self._span_of(reservation) == span):
                return reference
        return None

    # ------------------------------------------------------------ policies ---

    def publish_policy(self, restaurant_id, body, user_id, key) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        with self.store.lock:
            restaurant = self._restaurant(restaurant_id)
            if user_id not in restaurant["manager_user_ids"]:
                raise ApiError(403, "forbidden",
                               "only a manager of this restaurant may publish policies")
        return self._idempotent(user_id, key, "POST",
                                f"/restaurants/{restaurant_id}/policies", body,
                                lambda: self._publish_locked(restaurant_id, body))

    def _publish_locked(self, restaurant_id, body) -> tuple:
        with self.store.lock:
            restaurant = self.store.restaurants[restaurant_id]
            policy = self._validate_policy(restaurant, body)
            versions = [p["policy_version"]
                        for p in self.store.policies.get(restaurant_id, [])]
            policy["policy_version"] = (max(versions) + 1) if versions else 1
            self.store.policies.setdefault(restaurant_id, []).append(policy)
            if profile.STAGE >= 3:
                self.store.bump_restaurant_revision(restaurant_id)
            return 201, jsonable(policy)

    @staticmethod
    def _validate_policy(restaurant, body) -> dict:
        effective = body.get("effective_from")
        if effective is None:
            raise invalid("effective_from is required")
        if not isinstance(effective, str):
            raise malformed("effective_from must be a string")
        if t.parse_date(effective) is None:
            raise invalid("effective_from must be an actual YYYY-MM-DD date")
        grid = body.get("slot_minutes")
        duration = body.get("reservation_duration_minutes")
        cutoff = body.get("cancellation_cutoff_minutes")
        if not is_int(grid) or not 1 <= grid <= 1440:
            raise invalid("slot_minutes must be an integer 1..1440")
        if not is_int(duration) or not 1 <= duration <= 1440:
            raise invalid("reservation_duration_minutes must be an integer 1..1440")
        if not is_int(cutoff) or not 0 <= cutoff <= 10080:
            raise invalid("cancellation_cutoff_minutes must be an integer 0..10080")
        raw_hours = body.get("opening_hours")
        if raw_hours is None:
            raise invalid("opening_hours is required")
        if not isinstance(raw_hours, list):
            raise malformed("opening_hours must be a list of weekday entries")
        hours = store_mod.validate_opening_hours(raw_hours, "opening_hours")
        capacities = body.get("capacities")
        if capacities is None:
            raise invalid("capacities is required")
        if not isinstance(capacities, dict):
            raise malformed("capacities must be an object")
        expected = {table["id"] for table in restaurant["tables"]}
        if set(capacities) != expected:
            raise invalid("capacities must name exactly this restaurant's tables")
        for value in capacities.values():
            if not is_int(value) or not 1 <= value <= 100:
                raise invalid("each capacity must be an integer 1..100")
        return {"policy_version": 0, "effective_from": effective, "slot_minutes": grid,
                "reservation_duration_minutes": duration,
                "cancellation_cutoff_minutes": cutoff, "opening_hours": hours,
                "capacities": dict(capacities)}

    def list_policies(self, restaurant_id) -> tuple:
        with self.store.lock:
            self._restaurant(restaurant_id)
            if profile.STAGE < 3:
                raise not_found("no such restaurant")
            return 200, {"policies": [jsonable(p) for p in
                                      self.store.policies.get(restaurant_id, [])]}

    # -------------------------------------------------------------- series ---

    def create_series(self, body, user_id, key) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        return self._idempotent(user_id, key, "POST", "/series", body,
                                lambda: self._series_locked(body, user_id))

    def _series_locked(self, body, user_id) -> tuple:
        with self.store.lock:
            anchor_reference = body.get("anchor_reference")
            if not isinstance(anchor_reference, str) or not anchor_reference:
                raise invalid("anchor_reference is required")
            count, interval = body.get("count"), body.get("interval_weeks")
            if not is_int(count) or not 2 <= count <= 12:
                raise invalid("count must be an integer 2..12")
            if not is_int(interval) or not 1 <= interval <= 4:
                raise invalid("interval_weeks must be an integer 1..4")
            anchor = self.store.reservations.get(anchor_reference)
            if anchor is None or anchor["user_id"] != user_id:
                raise not_found("no such reservation")
            if anchor["status"] == "cancelled":
                raise ApiError(409, "reservation_cancelled",
                               "that booking is cancelled")
            if anchor.get("series_id"):
                raise ApiError(409, "already_in_series",
                               "that booking already belongs to a series")
            self._check_cutoff(anchor)
            restaurant = self.store.restaurants[anchor["restaurant_id"]]

            # Phase one: validate every generated occurrence. Nothing is written until
            # the whole adoption is known to succeed, so a failure at index 7 leaves no
            # partial series behind.
            anchor_date = t.parse_date(anchor["scheduled_date"])
            if anchor_date is None:
                raise invalid("that booking has no scheduled date")
            clock = t.parse_local(anchor["starts_at_local"])
            plan = []
            for index in range(1, count):
                scheduled = anchor_date + dt.timedelta(days=index * interval * 7)
                local_text = f"{scheduled.isoformat()}T" \
                             f"{t.format_hhmm(clock.hour * 60 + clock.minute)}"
                naive, instant, terms, zone = self._resolve_start(restaurant, local_text)
                self._check_slot(terms, zone, naive)
                self._check_capacity(terms, anchor["table_ids"], anchor["party_size"])
                end = instant + dt.timedelta(minutes=_duration_minutes(terms))
                busy = self._busy(restaurant["id"])
                if any(not self._free(busy, table_id, instant, end)
                       for table_id in anchor["table_ids"]):
                    raise ApiError(409, "table_unavailable",
                                   "an occurrence would overlap an existing booking")
                plan.append((index, scheduled, naive, instant, terms))

            # Phase two: commit.
            self.store.next_counter("series")
            series_id = f"ser_{self.store.counters['series']}"
            anchor["series_id"] = series_id
            anchor["series_index"] = 0
            occurrences = [{"index": 0, "reference": anchor["reference"],
                            "scheduled_date": anchor["scheduled_date"]}]
            for index, scheduled, naive, instant, terms in plan:
                occurrence = self._new_reservation(
                    user_id, restaurant, anchor["table_ids"], anchor["party_size"],
                    naive, instant, terms,
                    series=(series_id, index), bump=False)
                occurrences.append({"index": index,
                                    "reference": occurrence["reference"],
                                    "scheduled_date": scheduled.isoformat()})
            series = {"series_id": series_id, "user_id": user_id,
                      "restaurant_id": restaurant["id"], "interval_weeks": interval,
                      "revision": 1, "created_at": t.format_utc(t.now()),
                      "occurrences": occurrences}
            self.store.series[series_id] = series
            if profile.STAGE >= 3:
                self.store.bump_restaurant_revision(restaurant["id"])
            return 201, self._series_view(series)

    def _series_view(self, series) -> dict:
        occurrences = []
        for entry in series["occurrences"]:
            reservation = self.store.reservations[entry["reference"]]
            occurrences.append({"index": entry["index"],
                                "reference": entry["reference"],
                                "exception": bool(reservation.get("exception")),
                                "reservation": self._view(reservation)})
        return {"series_id": series["series_id"], "revision": series["revision"],
                "interval_weeks": series["interval_weeks"],
                "occurrences": occurrences}

    def get_series(self, series_id, user_id) -> tuple:
        with self.store.lock:
            series = self.store.series.get(series_id)
            if series is None or series["user_id"] != user_id or profile.STAGE < 3:
                raise not_found("no such series")
            return 200, self._series_view(series)

    def amend_series(self, series_id, body, user_id, key) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        with self.store.lock:
            series = self.store.series.get(series_id)
            if series is None or series["user_id"] != user_id:
                raise not_found("no such series")
        return self._idempotent(user_id, key, "POST",
                                f"/series/{series_id}/amend", body,
                                lambda: self._amend_series_locked(series_id, body))

    def _amend_series_locked(self, series_id, body) -> tuple:
        with self.store.lock:
            series = self.store.series[series_id]
            expected = body.get("expected_revision")
            if not is_int(expected) or expected < 1:
                raise invalid("expected_revision must be a positive integer")
            from_index = body.get("from_index")
            if not is_int(from_index) or not 0 <= from_index < len(
                    series["occurrences"]):
                raise invalid("from_index must be an integer inside the series")
            clock = t.parse_hhmm(body.get("local_time"))
            if clock is None:
                raise invalid("local_time must be exactly HH:MM between 00:00 and 23:59")
            if series["revision"] != expected:
                raise ApiError(409, "stale_revision",
                               "the series has moved on since you read it")
            restaurant = self.store.restaurants[series["restaurant_id"]]
            zone = t.zone(restaurant["timezone"])
            eligible = []
            for entry in series["occurrences"]:
                reservation = self.store.reservations[entry["reference"]]
                if entry["index"] < from_index or reservation["status"] == "cancelled" \
                        or reservation.get("exception"):
                    continue
                eligible.append((entry, reservation))

            # Validate everything before writing anything.
            plan = []
            for entry, reservation in eligible:
                local_text = f"{entry['scheduled_date']}T{t.format_hhmm(clock)}"
                naive, instant, terms, _ = self._resolve_start(restaurant, local_text)
                self._check_slot(terms, zone, naive)
                self._check_capacity(terms, reservation["table_ids"],
                                     reservation["party_size"])
                self._check_cutoff(reservation)
                plan.append((reservation, naive, instant, terms))

            listed = {reservation["reference"] for reservation, *_ in plan}
            outside = {}
            for table_id, spans in self._busy(restaurant["id"]).items():
                outside[table_id] = [
                    span for span in spans
                    if self._span_holder(restaurant["id"], table_id, span) not in listed]
            for reservation, naive, instant, terms in plan:
                end = instant + dt.timedelta(minutes=_duration_minutes(terms))
                for table_id in reservation["table_ids"]:
                    if not self._free(outside, table_id, instant, end):
                        raise ApiError(409, "table_unavailable",
                                       "an occurrence would overlap another booking")

            changed = False
            for reservation, naive, instant, terms in plan:
                wanted = {"table_ids": list(reservation["table_ids"]),
                          "party_size": reservation["party_size"],
                          "starts_at_local": f"{naive.date().isoformat()}T"
                                             f"{t.format_hhmm(clock)}"}
                if self._commit_amendment(reservation, wanted, instant, terms,
                                          series_effect="none"):
                    changed = True
            if changed:
                series["revision"] += 1
                self.store.bump_restaurant_revision(restaurant["id"])
            return 201, self._series_view(series)

    # ---------------------------------------------------------- replanning ---

    def create_replan(self, restaurant_id, body, user_id, key) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        with self.store.lock:
            restaurant = self._restaurant(restaurant_id)
            if user_id not in restaurant["manager_user_ids"]:
                raise ApiError(403, "forbidden",
                               "only a manager of this restaurant may plan closures")
        return self._idempotent(user_id, key, "POST",
                                f"/restaurants/{restaurant_id}/replans", body,
                                lambda: self._replan_locked(restaurant_id, body))

    def _replan_locked(self, restaurant_id, body) -> tuple:
        with self.store.lock:
            restaurant = self.store.restaurants[restaurant_id]
            table_id = body.get("table_id")
            if not isinstance(table_id, str):
                raise malformed("table_id must be a string")
            if table_id not in {table["id"] for table in restaurant["tables"]}:
                raise not_found("no such table at this restaurant")
            if body.get("from") is None or body.get("to") is None:
                raise invalid("from and to are required")
            start, end = t.parse_instant(body["from"]), t.parse_instant(body["to"])
            if start is None or end is None:
                raise invalid("from and to must be instants with an explicit offset")
            if not start < end:
                raise invalid("the closure interval must be non-empty")
            if len(restaurant["tables"]) > 6 or len(restaurant["combinable"]) > 4:
                raise ApiError(422, "planning_limit",
                               "this restaurant is larger than the planner supports")
            considered = [
                reservation for reservation in self.store.reservations.values()
                if reservation["restaurant_id"] == restaurant_id
                and reservation["status"] == "confirmed"
                and overlaps(t.parse_instant(reservation["starts_at"]),
                             t.parse_instant(reservation["ends_at"]), start, end)]
            if len(considered) > 6:
                raise ApiError(422, "planning_limit",
                               "more overlapping bookings than the planner supports")
            plan = self._plan(restaurant, table_id, start, end, considered)
            if plan is None:
                raise ApiError(409, "no_feasible_plan",
                               "no arrangement keeps every booking seated")
            self.store.next_counter("plan")
            record = {
                "plan_id": f"plan_{self.store.counters['plan']}",
                "restaurant_id": restaurant_id,
                # Echo the instants as they were sent: the caller's offset is part of
                # what it asked for, and the planner only needs the instants.
                "closure": {"table_id": table_id, "from": body["from"],
                            "to": body["to"]},
                "assignments": plan["assignments"],
                "moved_count": plan["moved_count"],
                "unused_seats": plan["unused_seats"],
                "restaurant_revision": self.store.restaurant_revision.get(
                    restaurant_id, 0),
                "created_at": t.format_utc(t.now()),
                "applied": False,
            }
            self.store.plans[record["plan_id"]] = record
            return 201, self._plan_view(record)

    def _plan_view(self, record) -> dict:
        return {"plan_id": record["plan_id"],
                "restaurant_revision": record["restaurant_revision"],
                "closure": dict(record["closure"]),
                "assignments": [dict(a) for a in record["assignments"]],
                "moved_count": record["moved_count"],
                "unused_seats": record["unused_seats"]}

    def _plan_options(self, restaurant, booking, closure, fixed) -> list:
        """`(rank, option)` for each seating this booking could take.

        Singles are ranked first in fixture order, then declared pairs in declaration
        order, starting at 0. An option is infeasible when it cannot seat the party
        under the booking's own accepted terms, or when it fights a fixed booking, a
        previously applied closure, or the proposed closure.
        """
        terms = booking.get("accepted_terms")
        if not terms:
            terms = terms_for_date(self._policy_state(), restaurant["id"],
                                   t.parse_date(booking["scheduled_date"]))
        capacities = terms["capacities"]
        start, end = self._span_of(booking)
        applied = [(c["table_id"], t.parse_instant(c["from"]), t.parse_instant(c["to"]))
                   for c in self.store.closures.get(restaurant["id"], [])]
        options, rank = [], 0
        candidates = [[table["id"]] for table in restaurant["tables"]]
        candidates += [list(pair) for pair in restaurant["combinable"]]
        for option in candidates:
            feasible = sum(capacities.get(member, 0) for member in option) \
                >= booking["party_size"]
            if feasible:
                for member in option:
                    if member == closure["table_id"]:
                        feasible = False
                    if any(overlaps(start, end, a, b) for a, b in fixed.get(member, [])):
                        feasible = False
                    if any(member == c_table and overlaps(start, end, c_from, c_to)
                           for c_table, c_from, c_to in applied):
                        feasible = False
            if feasible:
                options.append((rank, option))
            rank += 1
        return options

    def _plan(self, restaurant, closed_table, closure_start, closure_end, considered):
        """Fewest tables moved, then fewest unused seats, then lowest option ranks.

        Depth-first over the considered bookings in reservation-reference order, with
        each booking's current assignment tried first so low-change plans are found
        early and prune the tree. The node budget only matters for inputs far past the
        documented planning limits.
        """
        ordered = sorted(considered, key=lambda r: r["reference"])
        closure = {"table_id": closed_table, "from": closure_start, "to": closure_end}
        considered_refs = {r["reference"] for r in ordered}
        fixed = {table["id"]: [] for table in restaurant["tables"]}
        for reservation in self.store.reservations.values():
            if (reservation["restaurant_id"] != restaurant["id"]
                    or reservation["status"] != "confirmed"
                    or reservation["reference"] in considered_refs):
                continue
            span = self._span_of(reservation)
            for table_id in reservation["table_ids"]:
                fixed.setdefault(table_id, []).append(span)
        for closure_entry in self.store.closures.get(restaurant["id"], []):
            fixed.setdefault(closure_entry["table_id"], []).append(
                (t.parse_instant(closure_entry["from"]),
                 t.parse_instant(closure_entry["to"])))

        choices = []
        for booking in ordered:
            options = self._plan_options(restaurant, booking, closure, fixed)
            if not options:
                return None
            terms = booking.get("accepted_terms") or terms_for_date(
                self._policy_state(), restaurant["id"],
                t.parse_date(booking["scheduled_date"]))
            capacities = terms["capacities"]
            current = frozenset(booking["table_ids"])
            entries = []
            for rank, option in options:
                entries.append({
                    "rank": rank, "option": tuple(option),
                    "adds": 0 if frozenset(option) == current else 1,
                    "spend": sum(capacities.get(member, 0) for member in option)
                    - booking["party_size"],
                })
            entries.sort(key=lambda e: (e["adds"], e["rank"]))
            choices.append({"booking": booking, "entries": entries,
                            "start": t.parse_instant(booking["starts_at"]),
                            "end": t.parse_instant(booking["ends_at"]),
                            "floor": min(e["spend"] for e in entries)})
        suffix_floor = [0] * (len(choices) + 1)
        for index in range(len(choices) - 1, -1, -1):
            suffix_floor[index] = suffix_floor[index + 1] + choices[index]["floor"]

        best = {"score": None, "picks": None}
        budget = [400000]
        picks = [None] * len(choices)

        def walk(index, occupied, changes, unused):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            if best["score"] is not None and changes > best["score"][0]:
                return
            if index == len(choices):
                ranks = tuple(entries["rank"] for entries in picks)
                score = (changes, unused, ranks)
                if best["score"] is None or score < best["score"]:
                    best["score"] = score
                    best["picks"] = list(picks)
                return
            entry_set = choices[index]
            for entry in entry_set["entries"]:
                if (best["score"] is not None and changes + entry["adds"]
                        == best["score"][0]
                        and unused + entry["spend"] + suffix_floor[index + 1]
                        > best["score"][1]):
                    continue
                conflict = any(overlaps(entry_set["start"], entry_set["end"], a, b)
                               for member in entry["option"]
                               for a, b in occupied.get(member, []))
                if conflict:
                    continue
                for member in entry["option"]:
                    occupied.setdefault(member, []).append(
                        (entry_set["start"], entry_set["end"]))
                picks[index] = entry
                walk(index + 1, occupied, changes + entry["adds"],
                     unused + entry["spend"])
                picks[index] = None
                for member in entry["option"]:
                    occupied[member].pop()
                if budget[0] <= 0:
                    return

        walk(0, {table["id"]: list(fixed.get(table["id"], []))
                 for table in restaurant["tables"]}, 0, 0)
        if best["picks"] is None:
            return None
        assignments, moved, unused = [], 0, 0
        for entry_set, entry in zip(choices, best["picks"]):
            booking = entry_set["booking"]
            moved += entry["adds"]
            unused += entry["spend"]
            assignments.append({"reference": booking["reference"],
                                "table_ids": list(entry["option"]),
                                "changed": bool(entry["adds"])})
        return {"assignments": assignments, "moved_count": moved,
                "unused_seats": unused}

    def apply_replan(self, restaurant_id, plan_id, body, user_id, key) -> tuple:
        if not isinstance(body, dict):
            raise malformed("the request body must be a JSON object")
        with self.store.lock:
            restaurant = self._restaurant(restaurant_id)
            if user_id not in restaurant["manager_user_ids"]:
                raise ApiError(403, "forbidden",
                               "only a manager of this restaurant may apply plans")
        return self._idempotent(
            user_id, key, "POST",
            f"/restaurants/{restaurant_id}/replans/{plan_id}/apply", body,
            lambda: self._apply_replan_locked(restaurant_id, plan_id))

    def _apply_replan_locked(self, restaurant_id, plan_id) -> tuple:
        with self.store.lock:
            record = self.store.plans.get(plan_id)
            if record is None or record["restaurant_id"] != restaurant_id:
                raise not_found("no such plan")
            if record["applied"]:
                raise ApiError(409, "plan_already_applied",
                               "that plan has already been applied")
            if self.store.restaurant_revision.get(restaurant_id, 0) != record[
                    "restaurant_revision"]:
                raise ApiError(409, "stale_plan",
                               "this restaurant changed since the plan was previewed")
            self.store.closures.setdefault(restaurant_id, []).append(
                dict(record["closure"]))
            touched_series = set()
            for assignment in record["assignments"]:
                reservation = self.store.reservations[assignment["reference"]]
                if not assignment["changed"]:
                    continue
                previous = list(reservation["table_ids"])
                reservation["table_ids"] = list(assignment["table_ids"])
                if profile.STAGE >= 3:
                    zone = t.zone(self.store.restaurants[
                        reservation["restaurant_id"]]["timezone"])
                    reservation["revision"] += 1
                    reservation["history"].append(history_entry(
                        reservation, reservation["revision"], "reassigned",
                        [{"field": "table_ids", "from": previous,
                          "to": list(assignment["table_ids"])}],
                        plan_id=plan_id, zone=zone))
                    if reservation.get("series_id"):
                        touched_series.add(reservation["series_id"])
            if profile.STAGE >= 3:
                for series_id in touched_series:
                    self.store.series[series_id]["revision"] += 1
                self.store.bump_restaurant_revision(restaurant_id)
            record["applied"] = True
            views = [self._view(self.store.reservations[assignment["reference"]])
                     for assignment in record["assignments"]]
            return 201, {"plan_id": plan_id,
                         "restaurant_revision": self.store.restaurant_revision.get(
                             restaurant_id, 0),
                         "reservations": views}

    # ------------------------------------------------ test control surface ---

    def reset(self, fixture) -> tuple:
        with self.store.lock:
            self.store.restore(store_mod.build_fixture_state(fixture))
            return 204, None

    def export_state(self) -> tuple:
        with self.store.lock:
            return 200, self.store.export_payload()

    def import_state(self, payload) -> tuple:
        if not isinstance(payload, dict):
            raise malformed("the import body must be a JSON object")
        with self.store.lock:
            self.store.restore(store_mod.validate_import(payload))
            return 204, None

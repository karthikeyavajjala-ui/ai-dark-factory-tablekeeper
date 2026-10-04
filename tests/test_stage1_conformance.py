"""Stage 1 conformance: the specification's rules, stated as checks.

Written from `spec/stage-1.md` rather than from the service's behaviour. Each test
names the rule it pins, so a failure says which promise broke.
"""
from __future__ import annotations

import concurrent.futures
import json
import re

import pytest

import fixtures as fx

BOOKING_DATE = fx.booking_date()


def log_in(client, address=None):
    creds = address or fx.ADA
    body = client.ok("POST", "/auth/login",
                     body={"email": creds["email"], "password": creds["password"]})
    return body["token"]


def seed(client, fixture=None):
    fixture = fixture or fx.fixture()
    assert client.post("/_test/reset", body=fixture).status == 204
    return fixture


def book(client, token, key, table="t_2", time="19:00", date=None, party=4,
         restaurant="r_anker", status=201):
    return client.ok("POST", "/reservations", status=status, token=token, key=key, body={
        "restaurant_id": restaurant, "table_id": table,
        "starts_at_local": f"{date or BOOKING_DATE}T{time}", "party_size": party})


# --------------------------------------------------------------------------- wire

def test_health_is_exactly_the_contract(client):
    reply = client.get("/health")
    assert reply.status == 200 and reply.json() == {"status": "ok"}


def test_bodies_are_json_with_a_utf8_charset(client):
    seed(client)
    assert "application/json" in client.get("/restaurants").headers["Content-Type"]


def test_unknown_body_fields_and_query_parameters_are_ignored(client):
    seed(client)
    token = log_in(client)
    body = client.ok("POST", "/reservations", status=201, token=token, key="k-unknown", body={
        "restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
        "starts_at_local": f"{BOOKING_DATE}T19:00", "colour": "blue"})
    assert body["status"] == "confirmed"
    listing = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                               f"&party_size=4&sort=ascending")
    assert listing["slots"]


def test_unparseable_body_is_malformed_request(client):
    seed(client)
    token = log_in(client)
    client.fails("POST", "/reservations", 400, "malformed_request", token=token,
                 key="k-bad", raw=b"{not json", content_type="application/json")


def test_json_of_the_wrong_type_is_malformed_request(client):
    seed(client)
    token = log_in(client)
    # `restaurant_id` is a string field; a number is the wrong JSON type.
    client.fails("POST", "/reservations", 400, "malformed_request", token=token,
                 key="k-type", body={"restaurant_id": 5, "table_id": "t_2",
                                     "starts_at_local": f"{BOOKING_DATE}T19:00",
                                     "party_size": 4})


@pytest.mark.parametrize("party", ["4", True, None, 4.5])
def test_party_size_of_the_wrong_json_type_is_validation_failed(client, party):
    seed(client)
    token = log_in(client)
    client.fails("POST", "/reservations", 422, "validation_failed", token=token,
                 key="k-party", body={"restaurant_id": "r_anker", "table_id": "t_2",
                                      "starts_at_local": f"{BOOKING_DATE}T19:00",
                                      "party_size": party})


@pytest.mark.parametrize("stamp", ["2026-05-01 19:00", "2026-05-01T19:00+02:00",
                                   "2026-05-01T19:00Z", "19:00", "2026-05-01T19:00:00"])
def test_starts_at_local_must_be_a_bare_local_minute(client, stamp):
    seed(client)
    token = log_in(client)
    client.fails("POST", "/reservations", 422, "validation_failed", token=token,
                 key="k-stamp", body={"restaurant_id": "r_anker", "table_id": "t_2",
                                      "starts_at_local": stamp, "party_size": 4})


def test_missing_field_is_validation_failed(client):
    seed(client)
    token = log_in(client)
    client.fails("POST", "/reservations", 422, "validation_failed", token=token,
                 key="k-missing", body={"restaurant_id": "r_anker", "table_id": "t_2",
                                        "starts_at_local": f"{BOOKING_DATE}T19:00"})


# ---------------------------------------------------------------------- identity

def test_signup_login_and_the_stated_auth_failures(client):
    seed(client)
    created = client.ok("POST", "/auth/signup", status=201, body={
        "email": "new@example.com", "password": "a good password",
        "display_name": "New"})
    assert set(created) == {"user_id", "display_name", "token"}
    assert created["display_name"] == "New"
    client.fails("POST", "/auth/signup", 409, "email_taken", body={
        "email": "new@example.com", "password": "a good password", "display_name": "N"})
    client.fails("POST", "/auth/signup", 422, "validation_failed", body={
        "email": "new2@example.com", "password": "short", "display_name": "N"})
    client.fails("POST", "/auth/signup", 422, "validation_failed", body={
        "email": "not-an-address", "password": "a good password", "display_name": "N"})
    client.fails("POST", "/auth/login", 401, "unauthenticated", body={
        "email": "new@example.com", "password": "wrong password"})
    client.fails("POST", "/auth/login", 401, "unauthenticated", body={
        "email": "nobody@example.com", "password": "a good password"})
    client.fails("GET", "/reservations", 401, "unauthenticated")
    client.fails("GET", "/reservations", 401, "unauthenticated", token="not-a-token")


def test_seeded_users_can_log_in_and_keep_several_live_tokens(client):
    seed(client)
    first = log_in(client)
    second = client.ok("POST", "/auth/login", body={
        "email": fx.ADA["email"], "password": fx.ADA["password"]})["token"]
    assert first != second
    assert client.get("/reservations", token=first).status == 200
    assert client.get("/reservations", token=second).status == 200


def test_browsing_is_public_and_reservations_are_not(client):
    seed(client)
    assert client.get("/restaurants").status == 200
    assert client.get("/restaurants/r_anker").status == 200
    assert client.get(f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                      f"&party_size=2").status == 200
    for path in ("/reservations", "/reservation-moves"):
        assert client.get(path).status in (401, 405)


def test_restaurant_detail_mirrors_the_fixture(client):
    seed(client)
    detail = client.ok("GET", "/restaurants/r_anker")
    for field in ("id", "name", "timezone", "slot_minutes",
                  "reservation_duration_minutes", "cancellation_cutoff_minutes",
                  "opening_hours", "tables"):
        assert field in detail, field
    assert [table["id"] for table in detail["tables"]] == ["t_1", "t_2", "t_3"]
    client.fails("GET", "/restaurants/nope", 404, "not_found")


# ------------------------------------------------------------------- availability

def test_slots_are_a_grid_from_opening_that_fits_before_closing(client):
    seed(client)
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                              f"&party_size=2")
    assert slots["date"] == BOOKING_DATE and slots["timezone"] == "Europe/Berlin"
    starts = [slot["starts_at_local"] for slot in slots["slots"]]
    # opens 18:00, closes 23:00, 30-minute grid, 90-minute bookings -> last start 21:30
    assert starts == [f"{BOOKING_DATE}T{hhmm}" for hhmm in
                      ("18:00", "18:30", "19:00", "19:30", "20:00", "20:30",
                       "21:00", "21:30")]
    for slot in slots["slots"]:
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
                        slot["starts_at"])
        assert "+02:00" in slot["starts_at"]  # Berlin is still on summer time


def test_a_full_slot_still_appears_with_an_empty_table_list(client):
    seed(client)
    token = log_in(client)
    for index, table in enumerate(("t_1", "t_2", "t_3")):
        book(client, token, f"k-fill-{index}", table=table, time="19:00",
             party=2 if table == "t_1" else 4)
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                              f"&party_size=2")
    at_seven = [s for s in slots["slots"] if s["starts_at_local"].endswith("T19:00")][0]
    assert at_seven["available_table_ids"] == []


def test_capacity_filters_tables_in_fixture_order(client):
    seed(client)
    small = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                              f"&party_size=5")
    assert small["slots"][0]["available_table_ids"] == ["t_3"]
    big = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                            f"&party_size=2")
    assert big["slots"][0]["available_table_ids"] == ["t_1", "t_2", "t_3"]


def test_a_closed_day_has_no_slots(client):
    seed(client)
    # 2026-05-04 is a Monday; the fixture opens Thursday to Sunday.
    closed = fx.fixture(restaurants=[fx.restaurant(weekdays=("thu", "fri", "sat", "sun"))])
    seed(client, closed)
    body = client.ok("GET", "/availability?restaurant_id=r_anker&date=2026-05-04&party_size=2")
    assert body["slots"] == []


def test_availability_requires_all_three_parameters(client):
    seed(client)
    for query in ("", "?restaurant_id=r_anker", "?restaurant_id=r_anker&date=2026-05-01",
                  "?restaurant_id=r_anker&date=2026-05-01&party_size="):
        client.fails("GET", f"/availability{query}", 422, "validation_failed")
    client.fails("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                        f"&party_size=0", 422, "validation_failed")


@pytest.mark.parametrize("party", ["4.0", "1e9", "+4", "%204", "4%20", "four"])
def test_query_integers_are_plain_digits(client, party):
    seed(client)
    client.fails("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                        f"&party_size={party}", 422, "validation_failed")


# ---------------------------------------------------------------------- booking

def test_a_booking_echoes_its_terms_and_owns_a_reference(client):
    seed(client)
    token = log_in(client)
    body = book(client, token, "k-one")
    assert body["status"] == "confirmed"
    assert re.match(r"^[A-Z0-9]{6,12}$", body["reference"])
    assert body["starts_at_local"] == f"{BOOKING_DATE}T19:00"
    assert body["starts_at"] == f"{BOOKING_DATE}T19:00:00+02:00"
    assert body["ends_at"] == f"{BOOKING_DATE}T20:30:00+02:00"  # 90 minutes
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
                    body["created_at"])
    second = book(client, token, "k-two", time="20:30")
    assert second["reference"] != body["reference"]


def test_the_grid_and_the_opening_hours_are_separate_errors(client):
    seed(client)
    token = log_in(client)
    client.fails("POST", "/reservations", 422, "not_on_slot_grid", token=token, key="k-g",
                 body={"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
                       "starts_at_local": f"{BOOKING_DATE}T19:15"})
    client.fails("POST", "/reservations", 422, "outside_opening_hours", token=token,
                 key="k-h", body={"restaurant_id": "r_anker", "table_id": "t_2",
                                  "party_size": 4,
                                  "starts_at_local": f"{BOOKING_DATE}T17:00"})
    # 21:30 fits (ends at 23:00); 22:00 would run past closing.
    client.fails("POST", "/reservations", 422, "outside_opening_hours", token=token,
                 key="k-i", body={"restaurant_id": "r_anker", "table_id": "t_2",
                                  "party_size": 4,
                                  "starts_at_local": f"{BOOKING_DATE}T22:00"})


def test_ownership_capacity_and_overlap_failures(client):
    seed(client)
    ada, grace = log_in(client), log_in(client, fx.GRACE)
    book(client, ada, "k-ada")
    client.fails("POST", "/reservations", 409, "table_unavailable", token=ada, key="k-dup",
                 body={"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
                       "starts_at_local": f"{BOOKING_DATE}T19:30"})
    # The interval is half-open, so a booking starting exactly at the end is fine.
    later = book(client, ada, "k-abut", time="20:30")
    assert later["status"] == "confirmed"
    client.fails("POST", "/reservations", 422, "party_exceeds_capacity", token=ada,
                 key="k-small", body={"restaurant_id": "r_anker", "table_id": "t_1",
                                      "party_size": 4,
                                      "starts_at_local": f"{BOOKING_DATE}T19:00"})
    client.fails("POST", "/reservations", 404, "not_found", token=grace, key="k-ghost",
                 body={"restaurant_id": "r_anker", "table_id": "t_ghost", "party_size": 2,
                       "starts_at_local": f"{BOOKING_DATE}T19:00"})
    other = fx.restaurant(rid="r_other", name="Elsewhere", tables=[
        {"id": "t_9", "label": "9", "capacity": 4}])
    seed(client, fx.fixture(restaurants=[fx.restaurant(), other]))
    ada = log_in(client)          # a reset invalidates the tokens issued before it
    # t_2 belongs to r_anker, so it is not a table of r_other.
    client.fails("POST", "/reservations", 404, "not_found", token=ada, key="k-mix",
                 body={"restaurant_id": "r_other", "table_id": "t_2", "party_size": 4,
                       "starts_at_local": f"{BOOKING_DATE}T19:00"})
    client.fails("POST", "/reservations", 404, "not_found", token=ada, key="k-ghost-r",
                 body={"restaurant_id": "r_nope", "table_id": "t_2", "party_size": 4,
                       "starts_at_local": f"{BOOKING_DATE}T19:00"})


def test_a_booking_in_the_past_is_allowed(client):
    seed(client)
    token = log_in(client)
    body = book(client, token, "k-past", date="2020-05-01")
    assert body["status"] == "confirmed"


# ------------------------------------------------------------------ idempotency

def test_idempotency_key_shape_and_replays(client):
    seed(client)
    token = log_in(client)
    request = {"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
               "starts_at_local": f"{BOOKING_DATE}T19:00"}
    client.fails("POST", "/reservations", 400, "missing_idempotency_key", token=token,
                 body=request)
    reply = client.request("POST", "/reservations", token=token, key="", body=request)
    assert reply.status == 400 and reply.json()["error"]["code"] == "missing_idempotency_key"
    client.fails("POST", "/reservations", 422, "validation_failed", token=token,
                 key="x" * 256, body=request)

    first = client.post("/reservations", token=token, key="k-replay", body=request)
    assert first.status == 201
    replay = client.post("/reservations", token=token, key="k-replay", body=request)
    assert replay.status == 200
    assert replay.json() == first.json()
    assert len(client.ok("GET", "/reservations", token=token)["reservations"]) == 1
    # A replay is answered from the receipt, so a later cancellation does not change it.
    client.ok("POST", f"/reservations/{first.json()['reference']}/cancel", token=token)
    again = client.post("/reservations", token=token, key="k-replay", body=request)
    assert again.status == 200 and again.json() == first.json()


def test_the_same_key_with_a_different_body_is_a_conflict(client):
    seed(client)
    token = log_in(client)
    request = {"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
               "starts_at_local": f"{BOOKING_DATE}T19:00"}
    assert client.post("/reservations", token=token, key="k-clash",
                       body=request).status == 201
    changed = dict(request, party_size=2)
    client.fails("POST", "/reservations", 409, "idempotency_key_reuse", token=token,
                 key="k-clash", body=changed)
    # Idempotency is resolved before field validation, so an invalid body also conflicts.
    client.fails("POST", "/reservations", 409, "idempotency_key_reuse", token=token,
                 key="k-clash", body={"party_size": "nope"})


def test_a_key_is_scoped_to_its_user_and_its_path(client):
    seed(client)
    ada, grace = log_in(client), log_in(client, fx.GRACE)
    request = {"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
               "starts_at_local": f"{BOOKING_DATE}T19:00"}
    assert client.post("/reservations", token=ada, key="shared",
                       body=request).status == 201
    hers = {"restaurant_id": "r_anker", "table_id": "t_3", "party_size": 4,
            "starts_at_local": f"{BOOKING_DATE}T20:30"}
    # The same key string for another user is that user's first use, not a reuse.
    assert client.post("/reservations", token=grace, key="shared", body=hers).status == 201
    # A different path is a different request, even with the same key.
    reference = client.ok("GET", "/reservations", token=grace)["reservations"][0]["reference"]
    moves = {"moves": [{"reference": reference,
                        "starts_at_local": f"{BOOKING_DATE}T21:00"}]}
    assert client.post("/reservation-moves", token=grace, key="shared",
                       body=moves).status == 201


def test_a_key_after_a_4xx_is_free_again(client):
    seed(client)
    token = log_in(client)
    client.fails("POST", "/reservations", 422, "validation_failed", token=token,
                 key="k-free", body={"restaurant_id": "r_anker", "table_id": "t_2",
                                     "party_size": 0,
                                     "starts_at_local": f"{BOOKING_DATE}T19:00"})
    booked = book(client, token, "k-free")
    assert booked["status"] == "confirmed"


def test_concurrent_identical_requests_take_effect_once(client):
    seed(client)
    token = log_in(client)
    request = {"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
               "starts_at_local": f"{BOOKING_DATE}T19:00"}

    def send(_):
        return client.post("/reservations", token=token, key="k-race", body=request)

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        replies = list(pool.map(send, range(8)))
    statuses = sorted(reply.status for reply in replies)
    assert statuses.count(201) == 1 and statuses.count(200) == 7
    bodies = {json.dumps(reply.json(), sort_keys=True) for reply in replies}
    assert len(bodies) == 1
    assert len(client.ok("GET", "/reservations", token=token)["reservations"]) == 1


def test_concurrent_different_keys_cannot_double_book(client):
    seed(client)
    token = log_in(client)
    request = {"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
               "starts_at_local": f"{BOOKING_DATE}T19:00"}

    def send(index):
        return client.post("/reservations", token=token, key=f"k-race-{index}",
                           body=request)

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        replies = list(pool.map(send, range(8)))
    statuses = sorted(reply.status for reply in replies)
    assert statuses.count(201) == 1
    assert all(status in (201, 409) for status in statuses)
    assert len(client.ok("GET", "/reservations", token=token)["reservations"]) == 1


# ------------------------------------------------------------ cancel and amend

def test_cancel_frees_the_table_and_is_idempotent(client):
    seed(client)
    token = log_in(client)
    booking = book(client, token, "k-cancel")
    after_cancel = client.ok("POST", f"/reservations/{booking['reference']}/cancel",
                             token=token)
    assert after_cancel["status"] == "cancelled"
    again = client.ok("POST", f"/reservations/{booking['reference']}/cancel", token=token)
    assert again["status"] == "cancelled"
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                              f"&party_size=4")
    at_seven = [s for s in slots["slots"] if s["starts_at_local"].endswith("T19:00")][0]
    assert "t_2" in at_seven["available_table_ids"]


def test_a_booking_inside_the_cutoff_cannot_be_cancelled_or_changed(client):
    seed(client)
    token = log_in(client)
    # A booking that has already started is inside every cutoff, whatever the clock says.
    past = (fx.date.today() - fx.timedelta(days=1)).isoformat()
    body = book(client, token, "k-soon", date=past)
    client.fails("POST", f"/reservations/{body['reference']}/cancel", 409, "cutoff_passed",
                 token=token)
    client.fails("PATCH", f"/reservations/{body['reference']}", 409, "cutoff_passed",
                 token=token, body={"party_size": 2})


def test_amending_moves_the_booking_and_keeps_its_identity(client):
    seed(client)
    token = log_in(client)
    original = book(client, token, "k-amend")
    moved = client.ok("PATCH", f"/reservations/{original['reference']}", token=token,
                      body={"starts_at_local": f"{BOOKING_DATE}T20:00", "table_id": "t_3",
                            "party_size": 6})
    assert moved["status"] == "confirmed"
    assert moved["reference"] == original["reference"]
    assert moved["reservation_id"] == original["reservation_id"]
    assert moved["created_at"] == original["created_at"]
    assert moved["starts_at"] == f"{BOOKING_DATE}T20:00:00+02:00"
    assert moved["party_size"] == 6
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                              f"&party_size=4")
    at_seven = [s for s in slots["slots"] if s["starts_at_local"].endswith("T19:00")][0]
    assert "t_2" in at_seven["available_table_ids"]


def test_a_failed_amendment_changes_nothing(client):
    seed(client)
    ada, grace = log_in(client), log_in(client, fx.GRACE)
    mine = book(client, ada, "k-mine")
    book(client, grace, "k-theirs", table="t_3", time="20:00", party=6)
    client.fails("PATCH", f"/reservations/{mine['reference']}", 409, "table_unavailable",
                 token=ada, body={"table_id": "t_3", "starts_at_local": f"{BOOKING_DATE}T20:00"})
    after = client.ok("GET", f"/reservations/{mine['reference']}", token=ada)
    assert after["starts_at_local"] == f"{BOOKING_DATE}T19:00"
    assert after["table_id"] == "t_2"
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                              f"&party_size=4")
    at_seven = [s for s in slots["slots"] if s["starts_at_local"].endswith("T19:00")][0]
    assert "t_2" not in at_seven["available_table_ids"]


def test_a_cancelled_booking_cannot_be_amended(client):
    seed(client)
    token = log_in(client)
    booking = book(client, token, "k-gone")
    client.ok("POST", f"/reservations/{booking['reference']}/cancel", token=token)
    client.fails("PATCH", f"/reservations/{booking['reference']}", 409,
                 "reservation_cancelled", token=token, body={"party_size": 2})


# ------------------------------------------------------------------- listing

def test_the_listing_is_the_callers_and_is_newest_first(client):
    seed(client)
    ada, grace = log_in(client), log_in(client, fx.GRACE)
    early = book(client, ada, "k-early", time="19:00")
    late = book(client, ada, "k-late", time="20:30")
    client.ok("POST", f"/reservations/{early['reference']}/cancel", token=ada)
    book(client, grace, "k-hers", time="19:00")
    listed = client.ok("GET", "/reservations", token=ada)["reservations"]
    assert [item["reference"] for item in listed] == [late["reference"], early["reference"]]
    assert {item["status"] for item in listed} == {"confirmed", "cancelled"}
    assert client.fails("GET", f"/reservations/{late['reference']}", 404, "not_found",
                        token=grace)
    assert client.fails("POST", f"/reservations/{late['reference']}/cancel", 404,
                        "not_found", token=grace)
    assert len(client.ok("GET", "/reservations", token=grace)["reservations"]) == 1


def test_an_empty_listing_is_an_empty_list(client):
    seed(client)
    assert client.ok("GET", "/reservations", token=log_in(client)) == {"reservations": []}


# -------------------------------------------------------------------- DST

def dst_fixture(zone, opens="00:00", closes="06:00", duration=60, slot=30):
    return fx.fixture(restaurants=[fx.restaurant(
        rid="r_dst", name="Dst", timezone=zone, slot_minutes=slot, duration=duration,
        opens=opens, closes=closes, tables=[{"id": "t_1", "label": "1", "capacity": 4}])])


def slot_starts(client, date):
    return [s["starts_at_local"] for s in client.ok(
        "GET", f"/availability?restaurant_id=r_dst&date={date}&party_size=2")["slots"]]


def test_spring_forward_skips_the_missing_hour_in_berlin(client):
    seed(client, dst_fixture("Europe/Berlin"))
    starts = slot_starts(client, "2026-03-29")
    assert starts == [f"2026-03-29T{hhmm}" for hhmm in
                      ("00:00", "00:30", "01:00", "01:30", "03:00", "03:30", "04:00",
                       "04:30", "05:00")]
    token = log_in(client)
    for missing in ("02:00", "02:30"):
        client.fails("POST", "/reservations", 422, "invalid_local_time", token=token,
                     key=f"k-{missing}",
                     body={"restaurant_id": "r_dst", "table_id": "t_1", "party_size": 2,
                           "starts_at_local": f"2026-03-29T{missing}"})


def test_spring_forward_skips_the_missing_hour_in_new_york(client):
    seed(client, dst_fixture("America/New_York"))
    starts = slot_starts(client, "2026-03-08")
    assert "2026-03-08T02:00" not in starts and "2026-03-08T02:30" not in starts
    assert "2026-03-08T01:30" in starts and "2026-03-08T03:00" in starts
    token = log_in(client)
    client.fails("POST", "/reservations", 422, "invalid_local_time", token=token,
                 key="k-ny", body={"restaurant_id": "r_dst", "table_id": "t_1",
                                   "party_size": 2,
                                   "starts_at_local": "2026-03-08T02:30"})


def test_fall_back_uses_the_first_occurrence_in_berlin(client):
    seed(client, dst_fixture("Europe/Berlin", duration=90))
    slots = client.ok("GET", "/availability?restaurant_id=r_dst&date=2026-10-25"
                             "&party_size=2")["slots"]
    by_local = {slot["starts_at_local"]: slot for slot in slots}
    assert by_local["2026-10-25T02:00"]["starts_at"] == "2026-10-25T02:00:00+02:00"
    assert by_local["2026-10-25T01:30"]["starts_at"] == "2026-10-25T01:30:00+02:00"
    token = log_in(client)
    booked = book(client, token, "k-fall", restaurant="r_dst", table="t_1",
                  date="2026-10-25", time="01:30", party=2)
    # 90 absolute minutes from 01:30 +02:00 ends at 02:00 +01:00, not 03:00.
    assert booked["starts_at"] == "2026-10-25T01:30:00+02:00"
    assert booked["ends_at"] == "2026-10-25T02:00:00+01:00"


def test_fall_back_uses_the_first_occurrence_in_new_york(client):
    seed(client, dst_fixture("America/New_York", duration=60))
    slots = client.ok("GET", "/availability?restaurant_id=r_dst&date=2026-11-01"
                             "&party_size=2")["slots"]
    by_local = {slot["starts_at_local"]: slot for slot in slots}
    assert by_local["2026-11-01T01:00"]["starts_at"] == "2026-11-01T01:00:00-04:00"
    token = log_in(client)
    booked = book(client, token, "k-ny2", restaurant="r_dst", table="t_1",
                  date="2026-11-01", time="01:00", party=2)
    assert booked["ends_at"] == "2026-11-01T01:00:00-05:00"  # 60 absolute minutes later

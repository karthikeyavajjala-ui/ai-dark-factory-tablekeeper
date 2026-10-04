"""Stage 2 conformance: combined tables, and the shapes that must not regress.

Written from `spec/stage-2.md` §"Model", §"API", §"Combined tables".
"""
from __future__ import annotations

import fixtures as fx
from test_stage1_conformance import BOOKING_DATE, book, log_in, seed

COMBINABLE = [["t_1", "t_2"], ["t_2", "t_3"]]


def combined_client(client):
    """Reset with a restaurant that declares combinable pairs, and sign in."""
    seed(client, fx.fixture(restaurants=[fx.restaurant(combinable=COMBINABLE)]))
    return log_in(client)


def options(client, party, date=None, time=None):
    body = client.ok("GET", f"/availability?restaurant_id=r_anker"
                            f"&date={date or BOOKING_DATE}&party_size={party}")
    if time is None:
        return body["slots"][0]["available_options"]
    return [slot for slot in body["slots"]
            if slot["starts_at_local"].endswith("T" + time)][0]["available_options"]


def test_available_options_lists_singles_then_declared_pairs(client):
    combined_client(client)
    listed = options(client, 6)
    assert listed == [
        {"table_ids": ["t_3"], "capacity": 6},
        {"table_ids": ["t_1", "t_2"], "capacity": 6},
        {"table_ids": ["t_2", "t_3"], "capacity": 10},
    ]


def test_available_table_ids_still_holds_singles_only(client):
    combined_client(client)
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker"
                             f"&date={BOOKING_DATE}&party_size=6")["slots"]
    assert slots[0]["available_table_ids"] == ["t_3"]
    assert "available_options" in slots[0]


def test_a_pair_booking_occupies_both_tables(client):
    token = combined_client(client)
    created = client.ok("POST", "/reservations", status=201, token=token, key="k-pair",
                        body={"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
                              "starts_at_local": f"{BOOKING_DATE}T19:00", "party_size": 6})
    assert created["table_ids"] == ["t_1", "t_2"]
    assert "table_id" not in created            # only a set of one carries `table_id`
    assert created["party_size"] == 6
    assert options(client, 6, time="19:00") == [
        {"table_ids": ["t_3"], "capacity": 6}]   # t_1, t_2 gone: both pairs are blocked
    assert options(client, 2, time="19:00") == [
        {"table_ids": ["t_3"], "capacity": 6}]
    # The pair is not transitive: {t_1,t_3} is not a pair at all.
    client.fails("POST", "/reservations", 422, "combination_not_allowed", token=token,
                 key="k-diag", body={"restaurant_id": "r_anker",
                                     "table_ids": ["t_1", "t_3"], "party_size": 8,
                                     "starts_at_local": f"{BOOKING_DATE}T19:00"})


def test_a_single_booking_still_carries_table_id(client):
    token = combined_client(client)
    created = book(client, token, "k-single")
    assert created["table_ids"] == ["t_2"] and created["table_id"] == "t_2"


def test_combination_rules(client):
    token = combined_client(client)
    body = {"restaurant_id": "r_anker", "starts_at_local": f"{BOOKING_DATE}T19:00",
            "party_size": 4}
    client.fails("POST", "/reservations", 422, "combination_not_allowed", token=token,
                 key="k-unlisted", body=dict(body, table_ids=["t_1", "t_3"]))
    client.fails("POST", "/reservations", 422, "combination_not_allowed", token=token,
                 key="k-three", body=dict(body, table_ids=["t_1", "t_2", "t_3"],
                                          party_size=8))
    client.fails("POST", "/reservations", 422, "validation_failed", token=token,
                 key="k-dup", body=dict(body, table_ids=["t_1", "t_1"], party_size=4))
    client.fails("POST", "/reservations", 422, "validation_failed", token=token,
                 key="k-both", body=dict(body, table_id="t_1", table_ids=["t_2"],
                                         party_size=4))
    client.fails("POST", "/reservations", 422, "party_exceeds_capacity", token=token,
                 key="k-fat", body=dict(body, table_ids=["t_1", "t_2"], party_size=7))
    assert client.post("/reservations", token=token, key="k-fits",
                       body=dict(body, table_ids=["t_1", "t_2"], party_size=6)).status == 201


def test_an_amendment_can_take_a_pair_and_cancel_frees_it(client):
    token = combined_client(client)
    created = book(client, token, "k-amend-pair")
    amended = client.ok("PATCH", f"/reservations/{created['reference']}", token=token,
                        body={"table_ids": ["t_2", "t_3"], "party_size": 8})
    assert amended["table_ids"] == ["t_2", "t_3"] and "table_id" not in amended
    # t_2 and t_3 are held by the pair; only t_1 is left, and no pair is free.
    assert options(client, 2, time="19:00") == [
        {"table_ids": ["t_1"], "capacity": 2}]
    client.ok("POST", f"/reservations/{created['reference']}/cancel", token=token)
    assert options(client, 8, time="19:00") == [
        {"table_ids": ["t_2", "t_3"], "capacity": 10}]


def test_seeded_reservations_may_hold_a_pair_or_be_cancelled(client):
    seed(client, fx.fixture(restaurants=[fx.restaurant(combinable=COMBINABLE)],
                            reservations=[
                                {"id": "res_1", "reference": "PAIR01", "user_id": "u_ada",
                                 "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
                                 "starts_at_local": f"{BOOKING_DATE}T19:00",
                                 "party_size": 6},
                                {"id": "res_2", "reference": "GONE01", "user_id": "u_ada",
                                 "restaurant_id": "r_anker", "table_id": "t_3",
                                 "status": "cancelled", "party_size": 2,
                                 "starts_at_local": f"{BOOKING_DATE}T19:00"}]))
    token = log_in(client)
    listed = client.ok("GET", "/reservations", token=token)["reservations"]
    by_reference = {item["reference"]: item for item in listed}
    assert by_reference["PAIR01"]["table_ids"] == ["t_1", "t_2"]
    assert by_reference["PAIR01"]["status"] == "confirmed"
    assert by_reference["GONE01"]["status"] == "cancelled"
    # The cancelled seed frees t_3; the pair still holds t_1 and t_2.
    assert options(client, 2, time="19:00") == [{"table_ids": ["t_3"], "capacity": 6}]
    assert client.fails("POST", "/reservations", 409, "table_unavailable", token=token,
                        key="k-taken", body={"restaurant_id": "r_anker",
                                             "table_id": "t_1", "party_size": 2,
                                             "starts_at_local": f"{BOOKING_DATE}T19:00"})


def test_moves_take_table_ids_per_move(client):
    token = combined_client(client)
    first = book(client, token, "k-mv-1")
    second = book(client, token, "k-mv-2", time="20:30")
    moved = client.ok("POST", "/reservation-moves", status=201, token=token,
                      key="k-mv-batch",
                      body={"moves": [
                          {"reference": first["reference"],
                           "table_ids": ["t_1", "t_2"], "party_size": 6},
                          {"reference": second["reference"], "table_id": "t_3"}]})
    assert [item["reference"] for item in moved["reservations"]] == \
        [first["reference"], second["reference"]]
    assert moved["reservations"][0]["table_ids"] == ["t_1", "t_2"]
    assert moved["reservations"][1]["table_ids"] == ["t_3"]
    assert options(client, 6, time="19:00") == [{"table_ids": ["t_3"], "capacity": 6}]


def test_no_table_belongs_to_two_overlapping_bookings(client):
    token = combined_client(client)
    book(client, token, "k-hold", table="t_2")
    client.fails("POST", "/reservations", 409, "table_unavailable", token=token,
                 key="k-clash-pair",
                 body={"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
                       "starts_at_local": f"{BOOKING_DATE}T20:00", "party_size": 6})

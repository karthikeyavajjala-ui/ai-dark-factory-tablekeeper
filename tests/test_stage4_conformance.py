"""Stage 4 conformance: closure replanning and recurring amendments.

Written from `spec/stage-4.md`.
"""
from __future__ import annotations

import concurrent.futures

import fixtures as fx
from test_stage1_conformance import BOOKING_DATE, book, log_in, seed
from test_stage3_conformance import history, managed, series_body

MANAGER = fx.ADA
WINDOW = (f"{BOOKING_DATE}T18:00:00+02:00", f"{BOOKING_DATE}T23:00:00+02:00")


def replan(client, token, table="t_2", window=WINDOW, key="k-plan", status=201,
           restaurant="r_anker"):
    reply = client.request("POST", f"/restaurants/{restaurant}/replans", token=token,
                           key=key, body={"table_id": table, "from": window[0],
                                          "to": window[1]})
    assert reply.status == status, f"replan -> {reply.status} {reply.text}"
    return reply.json()


def apply_plan(client, token, plan_id, key="k-apply", status=201, restaurant="r_anker"):
    reply = client.request("POST", f"/restaurants/{restaurant}/replans/{plan_id}/apply",
                           token=token, key=key, body={})
    assert reply.status == status, f"apply -> {reply.status} {reply.text}"
    return reply.json()


def test_replans_are_a_manager_write(client):
    token = managed(client)
    window = {"table_id": "t_2", "from": WINDOW[0], "to": WINDOW[1]}
    client.fails("POST", "/restaurants/r_anker/replans", 401, "unauthenticated",
                 key="k-a", body=window)
    client.fails("POST", "/restaurants/r_anker/replans", 403, "forbidden",
                 token=log_in(client, fx.GRACE), key="k-b", body=window)
    client.fails("POST", "/restaurants/r_nope/replans", 404, "not_found",
                 token=token, key="k-d", body=window)
    client.fails("POST", "/restaurants/r_anker/replans", 400, "missing_idempotency_key",
                 token=token, body=window)
    client.fails("POST", "/restaurants/r_anker/replans", 404, "not_found",
                 token=token, key="k-e", body=dict(window, table_id="t_ghost"))
    for broken in ({**window, "to": WINDOW[0]}, {**window, "from": WINDOW[1]},
                   {**window, "from": "2026-10-11T18:00"},
                   {**window, "to": "later"}, {"table_id": "t_2"}):
        client.fails("POST", "/restaurants/r_anker/replans", 422, "validation_failed",
                     token=token, key="k-f", body=broken)


def test_a_preview_stores_a_plan_and_changes_nothing(client):
    token = managed(client)
    plan = replan(client, token, table="t_1")
    assert plan["restaurant_revision"] == 0
    assert plan["closure"] == {"table_id": "t_1", "from": WINDOW[0], "to": WINDOW[1]}
    assert plan["assignments"] == [] and plan["moved_count"] == 0
    assert plan["unused_seats"] == 0
    listed = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                              f"&party_size=2&explain=true")["slots"]
    at_seven = [s for s in listed if s["starts_at_local"].endswith("T19:00")][0]
    assert at_seven["available_table_ids"] == ["t_1", "t_2", "t_3"]
    assert client.ok("GET", "/reservations", token=token)["reservations"] == []


def test_the_plan_moves_what_the_closure_forces(client):
    token = managed(client)
    doomed = book(client, token, "k-doomed", table="t_1", party=2)
    staying = book(client, token, "k-staying", table="t_2")
    plan = replan(client, token, table="t_1")
    assert plan["restaurant_revision"] == 2          # two bookings so far
    # Assignments arrive in reference order, which is not the order they were booked.
    references = sorted([doomed["reference"], staying["reference"]])
    assert [item["reference"] for item in plan["assignments"]] == references
    by_reference = {item["reference"]: item for item in plan["assignments"]}
    assert by_reference[doomed["reference"]] == {"reference": doomed["reference"],
                                                 "table_ids": ["t_3"], "changed": True}
    assert by_reference[staying["reference"]] == {"reference": staying["reference"],
                                                  "table_ids": ["t_2"], "changed": False}
    assert plan["moved_count"] == 1
    assert plan["unused_seats"] == 4                 # t_3 seats 2, so four seats idle
    applied = apply_plan(client, token, plan["plan_id"], key="k-apply-1")
    assert applied["restaurant_revision"] == 3       # one plan, one increment
    assert applied["plan_id"] == plan["plan_id"]
    assert [item["reference"] for item in applied["reservations"]] == references
    moved = client.ok("GET", f"/reservations/{doomed['reference']}", token=token)
    assert moved["table_ids"] == ["t_3"] and moved["revision"] == 2
    assert moved["starts_at_local"] == doomed["starts_at_local"]
    assert moved["ends_at"] == doomed["ends_at"]
    assert moved["accepted_terms"] == doomed["accepted_terms"]
    untouched = client.ok("GET", f"/reservations/{staying['reference']}", token=token)
    assert untouched["revision"] == 1 and untouched["table_ids"] == ["t_2"]
    entries = history(client, token, doomed["reference"])["entries"]
    assert [entry["event"] for entry in entries] == ["created", "reassigned"]
    assert entries[1]["changes"] == [{"field": "table_ids", "from": ["t_1"],
                                      "to": ["t_3"]}]
    assert entries[1]["plan_id"] == plan["plan_id"]
    assert entries[1]["revision"] == 2


def test_a_tie_on_seats_goes_to_the_lower_ranked_option(client):
    seed(client, fx.fixture(restaurants=[fx.restaurant(
        managers=[MANAGER["id"]],
        tables=[{"id": "t_1", "label": "1", "capacity": 4},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 4}])]))
    token = log_in(client)
    booking = book(client, token, "k-tie", table="t_3")
    plan = replan(client, token, table="t_3")
    # Both free tables seat the party with no seat wasted: the earlier one wins.
    assert plan["assignments"][0]["table_ids"] == ["t_1"]
    assert plan["unused_seats"] == 0 and plan["moved_count"] == 1
    assert booking["table_ids"] == ["t_3"]


def test_a_closure_hides_the_table_and_refuses_new_bookings(client):
    token = managed(client)
    book(client, token, "k-shut", table="t_1", party=2)
    plan = replan(client, token, table="t_1")
    apply_plan(client, token, plan["plan_id"], key="k-apply-shut")
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                             f"&party_size=2&explain=true")["slots"]
    at_seven = [s for s in slots if s["starts_at_local"].endswith("T19:00")][0]
    assert "t_1" not in at_seven["available_table_ids"]
    closed = [entry for entry in at_seven["explain"] if entry["table_id"] == "t_1"][0]
    assert closed["available"] is False
    rules = {rule["rule"]: rule["holds"] for rule in closed["rules"]}
    assert rules == {"capacity": True, "no_overlap": False}
    client.fails("POST", "/reservations", 409, "table_unavailable", token=token,
                 key="k-into-closure", body={"restaurant_id": "r_anker",
                                             "table_id": "t_1", "party_size": 2,
                                             "starts_at_local": f"{BOOKING_DATE}T20:00"})
    # Outside the closure the table is bookable again.
    outside = client.ok("GET", f"/availability?restaurant_id=r_anker"
                               f"&date={fx.booking_date(1)}&party_size=2")["slots"]
    assert "t_1" in outside[0]["available_table_ids"]


def test_a_plan_is_applied_once_and_goes_stale(client):
    token = managed(client)
    book(client, token, "k-once", table="t_1", party=2)
    plan = replan(client, token, table="t_1")
    applied = apply_plan(client, token, plan["plan_id"])
    client.fails("POST", f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", 409,
                 "plan_already_applied", token=token, key="k-again", body={})
    replay = apply_plan(client, token, plan["plan_id"], key="k-apply", status=200)
    assert replay == applied
    # A later write invalidates any plan that has not been applied.
    stale = replan(client, token, table="t_1", key="k-stale-plan")
    book(client, token, "k-interrupt", time="20:30")
    client.fails("POST", f"/restaurants/r_anker/replans/{stale['plan_id']}/apply", 409,
                 "stale_plan", token=token, key="k-stale-apply", body={})
    client.fails("POST", "/restaurants/r_anker/replans/plan_999/apply", 404, "not_found",
                 token=token, key="k-ghost-plan", body={})


def test_a_closure_with_nowhere_to_move_is_refused(client):
    seed(client, fx.fixture(restaurants=[fx.restaurant(
        managers=[MANAGER["id"]],
        tables=[{"id": "t_1", "label": "1", "capacity": 2}])]))
    token = log_in(client)
    booking = book(client, token, "k-stuck", table="t_1", party=2)
    client.fails("POST", "/restaurants/r_anker/replans", 409, "no_feasible_plan",
                 token=token, key="k-no-plan",
                 body={"table_id": "t_1", "from": WINDOW[0], "to": WINDOW[1]})
    after = client.ok("GET", f"/reservations/{booking['reference']}", token=token)
    assert after["revision"] == 1 and after["table_ids"] == ["t_1"]


def test_a_closure_across_seven_bookings_is_a_planning_limit(client):
    tables = [{"id": f"t_{index}", "label": str(index), "capacity": 4}
              for index in range(1, 7)]
    seed(client, fx.fixture(restaurants=[fx.restaurant(managers=[MANAGER["id"]],
                                                      tables=tables)]))
    token = log_in(client)
    for index in range(6):
        book(client, token, f"k-crowd-{index}", table=f"t_{index + 1}", party=2)
    book(client, token, "k-crowd-6", table="t_1", time="20:30", party=2)
    client.fails("POST", "/restaurants/r_anker/replans", 422, "planning_limit",
                 token=token, key="k-limit",
                 body={"table_id": "t_1", "from": WINDOW[0], "to": WINDOW[1]})


def test_concurrent_applications_do_not_half_move_a_table(client):
    token = managed(client)
    book(client, token, "k-race-plan", table="t_1", party=2)
    plan = replan(client, token, table="t_1")

    def apply(index):
        return client.request("POST", f"/restaurants/r_anker/replans/"
                                      f"{plan['plan_id']}/apply", token=token,
                              key=f"k-race-{index}", body={})

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(apply, range(2)))
    assert sorted(reply.status for reply in replies) == [201, 409]
    listed = client.ok("GET", "/reservations", token=token)["reservations"]
    # `t_2` wastes two seats where `t_3` wastes four, so the plan chose `t_2`.
    assert [item["table_ids"] for item in listed] == [["t_2"]]


# ------------------------------------------------------------ series amendments

def test_series_amendment_changes_the_clock_time_on_each_own_date(client):
    token = managed(client)
    anchor = book(client, token, "k-anchor-am")
    series = series_body(client, token, anchor["reference"], count=3, key="k-am-series")
    body = {"expected_revision": 1, "from_index": 1, "local_time": "20:00"}
    amended = client.ok("POST", f"/series/{series['series_id']}/amend", status=201,
                        token=token, key="k-am-1", body=body)
    assert amended["revision"] == 2
    times = [item["reservation"]["starts_at_local"] for item in amended["occurrences"]]
    assert times == [f"{BOOKING_DATE}T19:00", f"{fx.booking_date(14)}T20:00",
                     f"{fx.booking_date(21)}T20:00"]
    assert [item["reservation"]["revision"] for item in amended["occurrences"]] == [1, 2, 2]
    assert all(item["exception"] is False for item in amended["occurrences"])
    second = amended["occurrences"][1]["reference"]
    entries = history(client, token, second)["entries"]
    assert [entry["event"] for entry in entries] == ["created", "changed"]
    assert entries[1]["changes"] == [
        {"field": "starts_at_local",
         "from": f"{fx.booking_date(14)}T19:00", "to": f"{fx.booking_date(14)}T20:00"}]
    replay = client.ok("POST", f"/series/{series['series_id']}/amend", status=200,
                       token=token, key="k-am-1", body=body)
    assert replay == amended
    client.fails("POST", f"/series/{series['series_id']}/amend", 409, "stale_revision",
                 token=token, key="k-am-stale",
                 body=dict(body, expected_revision=1, local_time="18:30"))


def test_series_amendment_validation_and_ownership(client):
    token = managed(client)
    anchor = book(client, token, "k-anchor-val")
    series = series_body(client, token, anchor["reference"], count=3, key="k-val-series")
    path = f"/series/{series['series_id']}/amend"
    good = {"expected_revision": 1, "from_index": 1, "local_time": "20:00"}
    client.fails("POST", path, 401, "unauthenticated", key="k-v0", body=good)
    client.fails("POST", path, 404, "not_found", token=log_in(client, fx.GRACE),
                 key="k-v1", body=good)
    client.fails("POST", "/series/ser_99/amend", 404, "not_found", token=token,
                 key="k-v2", body=good)
    client.fails("POST", path, 400, "missing_idempotency_key", token=token, body=good)
    client.fails("POST", path, 409, "stale_revision", token=token, key="k-v3",
                 body=dict(good, expected_revision=9, local_time="19:15"))
    for broken in (dict(good, expected_revision=0), dict(good, expected_revision=True),
                   dict(good, from_index=-1), dict(good, from_index=3),
                   dict(good, from_index=False), dict(good, local_time="7:00"),
                   dict(good, local_time="24:00"), dict(good, local_time="20:00:00")):
        client.fails("POST", path, 422, "validation_failed", token=token,
                     key="k-v4", body=broken)
    client.fails("POST", path, 422, "outside_opening_hours", token=token, key="k-v5",
                 body=dict(good, local_time="23:00"))


def test_series_amendment_skips_dead_occurrences_and_no_ops(client):
    token = managed(client)
    anchor = book(client, token, "k-anchor-skip")
    series = series_body(client, token, anchor["reference"], count=4, key="k-skip")
    third = series["occurrences"][2]["reference"]
    client.ok("PATCH", f"/reservations/{third}", token=token, body={"party_size": 2})
    client.ok("POST", f"/reservations/{series['occurrences'][3]['reference']}/cancel",
              token=token)
    before = client.ok("GET", f"/series/{series['series_id']}", token=token)
    assert before["revision"] == 3
    # Only occurrence 1 can move: 2 is an exception, 3 is cancelled, 0 is out of range.
    amended = client.ok("POST", f"/series/{series['series_id']}/amend", status=201,
                        token=token, key="k-skip-1",
                        body={"expected_revision": 3, "from_index": 1,
                              "local_time": "21:00"})
    assert amended["revision"] == 4
    assert [item["exception"] for item in amended["occurrences"]] == \
        [False, False, True, False]
    assert amended["occurrences"][3]["reservation"]["status"] == "cancelled"
    assert amended["occurrences"][2]["reservation"]["starts_at_local"].endswith("T19:00")
    assert amended["occurrences"][1]["reservation"]["starts_at_local"].endswith("T21:00")
    # Asking for the time an occurrence already has changes nothing at all.
    again = client.ok("POST", f"/series/{series['series_id']}/amend", status=201,
                      token=token, key="k-skip-2",
                      body={"expected_revision": 4, "from_index": 1,
                            "local_time": "21:00"})
    assert again["revision"] == 4
    assert again["occurrences"][1]["reservation"]["revision"] == 2


def test_a_seating_repair_keeps_series_membership(client):
    token = managed(client)
    anchor = book(client, token, "k-anchor-seat")
    series = series_body(client, token, anchor["reference"], count=2, key="k-seat-series")
    second = series["occurrences"][1]["reference"]
    client.ok("PATCH", f"/reservations/{second}", token=token, body={"party_size": 2})
    before = client.ok("GET", f"/series/{series['series_id']}", token=token)
    plan = replan(client, token, table="t_2", key="k-seat-plan")
    apply_plan(client, token, plan["plan_id"], key="k-seat-apply")
    after = client.ok("GET", f"/series/{series['series_id']}", token=token)
    assert after["revision"] == before["revision"] + 1
    assert [item["reference"] for item in after["occurrences"]] == \
        [item["reference"] for item in before["occurrences"]]
    assert [item["exception"] for item in after["occurrences"]] == [False, True]
    assert [item["reservation"]["starts_at_local"] for item in after["occurrences"]] == \
        [item["reservation"]["starts_at_local"] for item in before["occurrences"]]
    assert after["occurrences"][0]["reservation"]["table_ids"] == ["t_3"]
    assert after["occurrences"][0]["reservation"]["accepted_terms"] == \
        before["occurrences"][0]["reservation"]["accepted_terms"]
    assert after["occurrences"][1]["reservation"]["table_ids"] == ["t_2"]

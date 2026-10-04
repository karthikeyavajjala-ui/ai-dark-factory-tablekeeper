"""Stage 3 conformance: explanations, history, policies and recurring agreements.

Written from `spec/stage-3.md`.
"""
from __future__ import annotations

import concurrent.futures

import fixtures as fx
from test_stage1_conformance import BOOKING_DATE, book, log_in, seed

MANAGER = fx.ADA
COMBINABLE = [["t_1", "t_2"], ["t_2", "t_3"]]


def managed(client, **kw):
    """A restaurant managed by Ada, reset and signed in as Ada."""
    seed(client, fx.fixture(restaurants=[fx.restaurant(managers=[MANAGER["id"]], **kw)]))
    return log_in(client)


def publish(client, token, policy, key="k-policy", status=201, restaurant="r_anker"):
    reply = client.request("POST", f"/restaurants/{restaurant}/policies", token=token,
                           key=key, body=policy)
    assert reply.status == status, f"publish -> {reply.status} {reply.text}"
    return reply.json()


# ------------------------------------------------------------------- explain

def test_explain_reports_both_rules_for_every_table(client):
    seed(client)
    token = log_in(client)
    book(client, token, "k-explain", table="t_2")
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                             f"&party_size=4&explain=true")["slots"]
    at_seven = [s for s in slots if s["starts_at_local"].endswith("T19:00")][0]
    assert [entry["table_id"] for entry in at_seven["explain"]] == ["t_1", "t_2", "t_3"]
    assert [e["available"] for e in at_seven["explain"]] == [False, False, True]
    assert [e["policy_version"] for e in at_seven["explain"]] == [0, 0, 0]
    for entry in at_seven["explain"]:
        assert [rule["rule"] for rule in entry["rules"]] == ["capacity", "no_overlap"]
    by_id = {entry["table_id"]: entry for entry in at_seven["explain"]}
    assert by_id["t_1"]["rules"] == [{"rule": "capacity", "holds": False},
                                     {"rule": "no_overlap", "holds": True}]
    assert by_id["t_2"]["rules"] == [{"rule": "capacity", "holds": True},
                                     {"rule": "no_overlap", "holds": False}]
    assert at_seven["available_table_ids"] == [e["table_id"] for e in at_seven["explain"]
                                              if e["available"]] == ["t_3"]


def test_explain_without_the_flag_keeps_the_stage_one_shape(client):
    seed(client)
    slots = client.ok("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                             f"&party_size=4")["slots"]
    assert all("explain" not in slot for slot in slots)


def test_explain_only_accepts_true(client):
    seed(client)
    for value in ("false", "1", "", "True", "yes"):
        client.fails("GET", f"/availability?restaurant_id=r_anker&date={BOOKING_DATE}"
                            f"&party_size=4&explain={value}", 422, "validation_failed")


def test_a_closed_day_has_no_slots_even_with_explain(client):
    seed(client, fx.fixture(restaurants=[fx.restaurant(weekdays=("thu",))]))
    body = client.ok("GET", "/availability?restaurant_id=r_anker&date=2026-05-04"
                            "&party_size=2&explain=true")
    assert body["slots"] == []


# ------------------------------------------------------------------- history

def history(client, token, reference, status=200):
    reply = client.request("GET", f"/reservations/{reference}/history", token=token)
    assert reply.status == status, f"history -> {reply.status} {reply.text}"
    return reply.json() if status == 200 else reply.json()


def test_history_records_creation_change_and_cancellation(client):
    seed(client)
    token = log_in(client)
    created = book(client, token, "k-hist")
    no_op = client.ok("PATCH", f"/reservations/{created['reference']}", token=token,
                      body={"party_size": 4, "table_ids": ["t_2"]})
    assert no_op["party_size"] == 4 and no_op["revision"] == 1
    client.ok("PATCH", f"/reservations/{created['reference']}", token=token,
              body={"table_id": "t_3", "starts_at_local": f"{BOOKING_DATE}T20:00"})
    client.ok("POST", f"/reservations/{created['reference']}/cancel", token=token)
    entries = history(client, token, created["reference"])["entries"]
    assert [entry["seq"] for entry in entries] == [1, 2, 3]
    assert [entry["event"] for entry in entries] == ["created", "changed", "cancelled"]
    assert entries[0]["changes"] == [
        {"field": "table_id", "from": None, "to": "t_2"},
        {"field": "starts_at_local", "from": None, "to": f"{BOOKING_DATE}T19:00"},
        {"field": "party_size", "from": None, "to": 4}]
    assert entries[1]["changes"] == [
        {"field": "table_id", "from": "t_2", "to": "t_3"},
        {"field": "starts_at_local", "from": f"{BOOKING_DATE}T19:00",
         "to": f"{BOOKING_DATE}T20:00"}]
    assert entries[2]["changes"] == []
    assert [entry["revision"] for entry in entries] == [1, 2, 3]
    for entry in entries:
        assert entry["accepted_terms"]["policy_version"] == 0
        assert entry["at"].startswith(f"{BOOKING_DATE[:4]}-")


def test_a_replay_records_no_history(client):
    seed(client)
    token = log_in(client)
    request = {"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
               "starts_at_local": f"{BOOKING_DATE}T19:00"}
    first = client.post("/reservations", token=token, key="k-replay-hist", body=request)
    client.post("/reservations", token=token, key="k-replay-hist", body=request)
    assert len(history(client, token, first.json()["reference"])["entries"]) == 1


def test_history_is_owner_only_even_without_a_token(client):
    seed(client)
    ada, grace = log_in(client), log_in(client, fx.GRACE)
    created = book(client, ada, "k-private")
    client.fails("GET", f"/reservations/{created['reference']}/history", 404, "not_found",
                 token=grace)
    client.fails("GET", f"/reservations/{created['reference']}/history", 404, "not_found")
    client.fails("GET", f"/reservations/{created['reference']}/decision", 404, "not_found")
    decision = client.ok("GET", f"/reservations/{created['reference']}/decision", token=ada)
    assert decision == {"reference": created["reference"], "revision": 1,
                        "accepted_terms": created["accepted_terms"]}
    client.ok("POST", f"/reservations/{created['reference']}/cancel", token=ada)
    after = client.ok("GET", f"/reservations/{created['reference']}/decision", token=ada)
    assert after["revision"] == 2


# ------------------------------------------------------------------ policies

def test_only_a_declared_manager_may_publish(client):
    seed(client, fx.fixture(restaurants=[fx.restaurant(managers=[MANAGER["id"]])]))
    policy = fx.policy("2026-09-28")
    client.fails("POST", "/restaurants/r_anker/policies", 401, "unauthenticated",
                 key="k-p1", body=policy)
    client.fails("POST", "/restaurants/r_anker/policies", 403, "forbidden",
                 token=log_in(client, fx.GRACE), key="k-p2", body=policy)
    client.fails("POST", "/restaurants/r_nope/policies", 404, "not_found",
                 token=log_in(client), key="k-p3", body=policy)
    published = publish(client, log_in(client), policy, key="k-p4")
    assert published["policy_version"] == 1


def test_policy_versions_count_per_restaurant_and_replays_allocate_none(client):
    token = managed(client)
    policy = fx.policy("2026-10-01")
    first = publish(client, token, policy, key="k-v1")
    assert first["policy_version"] == 1 and first["effective_from"] == "2026-10-01"
    replay = publish(client, token, policy, key="k-v1", status=200)
    assert replay == first
    second = publish(client, token, fx.policy("2026-10-05"), key="k-v2")
    assert second["policy_version"] == 2
    listed = client.ok("GET", "/restaurants/r_anker/policies")["policies"]
    assert [item["policy_version"] for item in listed] == [1, 2]  # publication order
    assert all(item["policy_version"] != 0 for item in listed)    # policy 0 stays implicit


def test_invalid_policies_are_rejected_without_a_version(client):
    token = managed(client)
    good = fx.policy("2026-10-01")
    for broken in ({key: value for key, value in good.items() if key != "capacities"},
                   dict(good, slot_minutes=True),
                   dict(good, slot_minutes=0),
                   dict(good, reservation_duration_minutes=1441),
                   dict(good, cancellation_cutoff_minutes=-1),
                   dict(good, cancellation_cutoff_minutes=10081),
                   dict(good, effective_from="2026-02-30"),
                   dict(good, effective_from="28-09-2026"),
                   dict(good, capacities={"t_1": 2, "t_2": 4}),
                   dict(good, capacities={"t_1": 2, "t_2": 4, "t_3": 0}),
                   dict(good, capacities={"t_1": True, "t_2": 4, "t_3": 6}),
                   dict(good, opening_hours=[{"weekday": "mon", "opens": "18:00",
                                              "closes": "23:00"},
                                             {"weekday": "mon", "opens": "12:00",
                                              "closes": "14:00"}])):
        reply = client.request("POST", "/restaurants/r_anker/policies", token=token,
                               key="k-bad", body=broken)
        assert reply.status == 422, f"{broken} -> {reply.status} {reply.text}"
        assert reply.json()["error"]["code"] == "validation_failed"
    assert client.ok("GET", "/restaurants/r_anker/policies")["policies"] == []
    assert publish(client, token, good, key="k-good")["policy_version"] == 1


def test_the_policy_for_a_date_is_the_latest_effective_one(client):
    token = managed(client)
    publish(client, token, fx.policy("2026-10-01", duration=90), key="k-a")
    publish(client, token, fx.policy("2026-10-20", duration=120), key="k-b")
    publish(client, token, fx.policy("2026-10-20", duration=60, cutoff=30), key="k-c")
    # Before every policy: policy 0 still governs.
    early = client.ok("GET", "/availability?restaurant_id=r_anker&date=2026-09-30"
                             "&party_size=4&explain=true")
    assert late_slot(early, "18:00")["explain"][1]["policy_version"] == 0
    # On the tie date the later publication wins.
    tie = client.ok("GET", "/availability?restaurant_id=r_anker&date=2026-10-20"
                           "&party_size=4&explain=true")
    assert late_slot(tie, "18:00")["explain"][1]["policy_version"] == 3
    # 60-minute bookings: the last 30-minute grid slot starts at 22:00.
    tie_starts = [slot["starts_at_local"][-5:] for slot in tie["slots"]]
    assert tie_starts[-1] == "22:00", tie_starts
    one_day_before = client.ok("GET", "/availability?restaurant_id=r_anker"
                                      "&date=2026-10-19&party_size=4")["slots"]
    assert one_day_before[-1]["starts_at_local"][-5:] == "21:30"  # 90-minute policy 1


def late_slot(body, time):
    return [slot for slot in body["slots"] if slot["starts_at_local"].endswith("T" + time)][0]


def test_a_booking_keeps_the_terms_it_accepted(client):
    token = managed(client)
    created = book(client, token, "k-terms")
    assert created["accepted_terms"] == {
        "policy_version": 0, "slot_minutes": 30, "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [{"weekday": day, "opens": "18:00", "closes": "23:00"}
                          for day in fx.WEEKDAYS],
        "capacities": {"t_1": 2, "t_2": 4, "t_3": 6}}
    assert created["revision"] == 1
    publish(client, token, fx.policy(BOOKING_DATE, duration=240, cutoff=10080),
            key="k-later")
    after = client.ok("GET", f"/reservations/{created['reference']}", token=token)
    assert after["accepted_terms"] == created["accepted_terms"]
    assert after["ends_at"] == created["ends_at"]
    assert after["revision"] == 1
    entries = history(client, token, created["reference"])["entries"]
    assert [entry["event"] for entry in entries] == ["created"]


def test_an_amendment_adopts_the_policy_of_its_new_date(client):
    token = managed(client)
    effective = fx.booking_date(20)
    publish(client, token, fx.policy(effective, duration=60), key="k-adopt")
    created = book(client, token, "k-old-date")
    amended = client.ok("PATCH", f"/reservations/{created['reference']}", token=token,
                        body={"starts_at_local": f"{effective}T19:00"})
    assert amended["accepted_terms"]["policy_version"] == 1
    assert amended["accepted_terms"]["reservation_duration_minutes"] == 60
    assert amended["ends_at"][:16] == f"{effective}T20:00"
    assert amended["revision"] == 2
    entries = history(client, token, created["reference"])["entries"]
    assert [entry["event"] for entry in entries] == ["created", "changed"]
    assert entries[0]["accepted_terms"]["policy_version"] == 0
    assert entries[1]["accepted_terms"]["policy_version"] == 1


def test_the_old_cutoff_governs_before_the_new_policy_is_checked(client):
    # Accepted terms carry a week-long cutoff; today's policy would allow anything.
    token = managed(client, cutoff=10080)
    soon = book(client, token, "k-soon-terms", date=fx.booking_date(2))
    far = book(client, token, "k-far-terms", date=fx.booking_date(30), time="20:30")
    publish(client, token, fx.policy("2020-01-01", cutoff=0), key="k-zero")
    client.fails("PATCH", f"/reservations/{soon['reference']}", 409, "cutoff_passed",
                 token=token, body={"party_size": 2})
    client.fails("POST", f"/reservations/{soon['reference']}/cancel", 409,
                 "cutoff_passed", token=token)
    changed = client.ok("PATCH", f"/reservations/{far['reference']}", token=token,
                        body={"party_size": 2})
    assert changed["accepted_terms"]["policy_version"] == 1
    assert changed["accepted_terms"]["cancellation_cutoff_minutes"] == 0


def test_expected_revision_gates_an_amendment(client):
    token = managed(client)
    created = book(client, token, "k-rev")
    client.fails("PATCH", f"/reservations/{created['reference']}", 422,
                 "validation_failed", token=token,
                 body={"party_size": 2, "expected_revision": 0})
    client.fails("PATCH", f"/reservations/{created['reference']}", 422,
                 "validation_failed", token=token,
                 body={"party_size": 2, "expected_revision": True})
    client.fails("PATCH", f"/reservations/{created['reference']}", 409, "stale_revision",
                 token=token, body={"party_size": 2, "expected_revision": 7})
    changed = client.ok("PATCH", f"/reservations/{created['reference']}", token=token,
                        body={"party_size": 2, "expected_revision": 1})
    assert changed["revision"] == 2
    client.fails("PATCH", f"/reservations/{created['reference']}", 409, "stale_revision",
                 token=token, body={"party_size": 4, "expected_revision": 1})


def test_one_amendment_wins_a_race_on_one_revision(client):
    token = managed(client)
    created = book(client, token, "k-race-rev")

    def amend(party):
        return client.request("PATCH", f"/reservations/{created['reference']}",
                              token=token, body={"party_size": party,
                                                 "expected_revision": 1})

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(amend, (2, 3)))
    assert sorted(reply.status for reply in replies) == [200, 409]


def test_the_restaurant_detail_still_shows_the_fixture_configuration(client):
    token = managed(client)
    publish(client, token, fx.policy("2020-01-01", slot_minutes=15, duration=30,
                                     cutoff=0, closes="22:00"), key="k-detail")
    detail = client.ok("GET", "/restaurants/r_anker")
    assert detail["slot_minutes"] == 30
    assert detail["reservation_duration_minutes"] == 90
    assert detail["cancellation_cutoff_minutes"] == 120
    assert detail["opening_hours"][0]["closes"] == "23:00"
    listed = client.ok("GET", "/restaurants/r_anker/policies")["policies"]
    assert listed[0]["slot_minutes"] == 15 and listed[0]["effective_from"] == "2020-01-01"


# --------------------------------------------------------------------- series

def series_body(client, token, anchor, count=3, interval=1, key="k-series"):
    reply = client.request("POST", "/series", token=token, key=key,
                           body={"anchor_reference": anchor, "count": count,
                                 "interval_weeks": interval})
    assert reply.status == 201, f"series -> {reply.status} {reply.text}"
    return reply.json()


def test_occurrences_land_on_the_same_clock_time_each_week(client):
    token = managed(client)
    anchor = book(client, token, "k-anchor")
    series = series_body(client, token, anchor["reference"], count=3)
    assert series["interval_weeks"] == 1 and series["revision"] == 1
    assert [item["index"] for item in series["occurrences"]] == [0, 1, 2]
    assert all(item["exception"] is False for item in series["occurrences"])
    dates = [item["reservation"]["starts_at_local"] for item in series["occurrences"]]
    assert dates[0] == f"{BOOKING_DATE}T19:00"
    for offset, stamp in zip((7, 14), dates[1:]):
        assert stamp == f"{fx.booking_date(7 + offset)}T19:00"
    assert series["occurrences"][0]["reference"] == anchor["reference"]
    assert series["occurrences"][0]["reservation"]["revision"] == 1
    references = [item["reference"] for item in series["occurrences"]]
    assert len(set(references)) == 3
    now = client.ok("GET", f"/series/{series['series_id']}", token=token)
    assert [item["reference"] for item in now["occurrences"]] == references
    listed = client.ok("GET", "/reservations", token=token)["reservations"]
    assert sorted(item["reference"] for item in listed) == sorted(references)
    for slot in client.ok("GET", f"/availability?restaurant_id=r_anker"
                                 f"&date={fx.booking_date(14)}&party_size=4")["slots"]:
        if slot["starts_at_local"].endswith("T19:00"):
            assert "t_2" not in slot["available_table_ids"]


def test_series_inputs_are_checked(client):
    token = managed(client)
    seed(client, fx.fixture(restaurants=[fx.restaurant(managers=[MANAGER["id"]])]))
    token = log_in(client)
    anchor = book(client, token, "k-anchor-2")
    body = {"anchor_reference": anchor["reference"], "count": 2, "interval_weeks": 1}
    client.fails("POST", "/series", 400, "missing_idempotency_key", token=token, body=body)
    for broken in (dict(body, count=1), dict(body, count=13), dict(body, count=True),
                   dict(body, interval_weeks=0), dict(body, interval_weeks=5),
                   dict(body, interval_weeks=False)):
        client.fails("POST", "/series", 422, "validation_failed", token=token,
                     key="k-bad-series", body=broken)
    client.fails("POST", "/series", 404, "not_found", token=token, key="k-ghost-s",
                 body=dict(body, anchor_reference="NOSUCH"))
    client.fails("POST", "/series", 404, "not_found", token=log_in(client, fx.GRACE),
                 key="k-foreign", body=body)
    client.fails("POST", "/series", 401, "unauthenticated", key="k-noauth", body=body)
    adopted = series_body(client, token, anchor["reference"], count=2, key="k-adopt")
    client.fails("POST", "/series", 409, "already_in_series", token=token,
                 key="k-again", body=body)
    client.ok("POST", f"/reservations/{adopted['occurrences'][1]['reference']}/cancel",
              token=token)
    cancelled = client.ok("GET", f"/series/{adopted['series_id']}", token=token)
    assert [item["reservation"]["status"] for item in cancelled["occurrences"]] == \
        ["confirmed", "cancelled"]
    assert cancelled["revision"] == 2        # a cancellation moves the series on
    assert all(item["exception"] is False for item in cancelled["occurrences"])
    client.fails("GET", f"/series/{adopted['series_id']}", 404, "not_found",
                 token=log_in(client, fx.GRACE))
    client.fails("GET", f"/series/{adopted['series_id']}", 404, "not_found")


def test_an_exception_and_a_no_op_are_told_apart(client):
    token = managed(client)
    anchor = book(client, token, "k-anchor-3")
    series = series_body(client, token, anchor["reference"], count=2, key="k-exc")
    second = series["occurrences"][1]["reference"]
    client.ok("PATCH", f"/reservations/{second}", token=token, body={"party_size": 4})  # no-op
    assert client.ok("GET", f"/series/{series['series_id']}",
                     token=token)["revision"] == 1
    client.ok("PATCH", f"/reservations/{second}", token=token, body={"party_size": 2})
    after = client.ok("GET", f"/series/{series['series_id']}", token=token)
    assert after["revision"] == 2
    assert [item["exception"] for item in after["occurrences"]] == [False, True]
    client.ok("POST", f"/reservations/{anchor['reference']}/cancel", token=token)
    last = client.ok("GET", f"/series/{series['series_id']}", token=token)
    assert last["revision"] == 3
    assert [item["reservation"]["status"] for item in last["occurrences"]] == \
        ["cancelled", "confirmed"]
    assert [item["exception"] for item in last["occurrences"]] == [False, True]


def test_a_nonexistent_local_time_rejects_the_whole_adoption(client):
    # 2027-02-28 02:30 exists; four weeks later the clocks have skipped 02:30.
    seed(client, fx.fixture(restaurants=[fx.restaurant(opens="00:00", closes="06:00")]))
    token = log_in(client)
    anchor = book(client, token, "k-gap", date="2027-02-28", time="02:30", party=2)
    client.fails("POST", "/series", 422, "invalid_local_time", token=token, key="k-gap-s",
                 body={"anchor_reference": anchor["reference"], "count": 3,
                       "interval_weeks": 4})
    listed = client.ok("GET", "/reservations", token=token)["reservations"]
    assert [item["reference"] for item in listed] == [anchor["reference"]]
    assert listed[0]["revision"] == 1
    assert len(history(client, token, anchor["reference"])["entries"]) == 1
    # Nothing was claimed, so the key is reusable for a different body that works.
    ok = series_body(client, token, anchor["reference"], count=3, interval=1,
                     key="k-gap-s")
    assert len(ok["occurrences"]) == 3


def test_each_occurrence_selects_its_own_date_policy(client):
    token = managed(client)
    anchor = book(client, token, "k-policy-occ")
    later = fx.booking_date(21)
    publish(client, token, fx.policy(later, duration=180), key="k-occ-policy")
    series = series_body(client, token, anchor["reference"], count=3, key="k-occ")
    reservations = [item["reservation"] for item in series["occurrences"]]
    assert [r["accepted_terms"]["policy_version"] for r in reservations] == [0, 0, 1]
    assert reservations[2]["starts_at_local"] == f"{later}T19:00"
    assert reservations[2]["ends_at"][:16] == f"{later}T22:00"   # 180 minutes
    assert reservations[1]["ends_at"][:16] == f"{fx.booking_date(14)}T20:30"


def test_a_replayed_adoption_returns_the_first_answer(client):
    token = managed(client)
    anchor = book(client, token, "k-anchor-4")
    body = {"anchor_reference": anchor["reference"], "count": 2, "interval_weeks": 1}
    first = client.post("/series", token=token, key="k-replay-s", body=body)
    assert first.status == 201
    client.ok("POST", f"/reservations/{anchor['reference']}/cancel", token=token)
    replay = client.post("/series", token=token, key="k-replay-s", body=body)
    assert replay.status == 200 and replay.json() == first.json()


def test_a_pair_series_keeps_its_tables_and_its_history_fields(client):
    seed(client, fx.fixture(restaurants=[fx.restaurant(combinable=COMBINABLE,
                                                       managers=[MANAGER["id"]])]))
    token = log_in(client)
    created = client.ok("POST", "/reservations", status=201, token=token, key="k-pair-s",
                        body={"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
                              "starts_at_local": f"{BOOKING_DATE}T19:00", "party_size": 6})
    series_body(client, token, created["reference"], count=2, key="k-pair-adopt")
    entries = history(client, token, created["reference"])["entries"]
    assert entries[0]["changes"][0] == {"field": "table_ids", "from": None,
                                        "to": ["t_1", "t_2"]}

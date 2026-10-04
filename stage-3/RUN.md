# Running stage 3

The service is one container. It reads `PORT` (default `8080`), binds `0.0.0.0`, and
needs no configuration file, no volume, no database and no outbound network.

## Build

```sh
docker build -t tablekeeper-stage3 .
```

## Start

```sh
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage3
```

Any port works; `PORT` is what the service listens on inside the container and the
mapped port is what you reach it on:

```sh
docker run --rm -p 9090:9090 -e PORT=9090 tablekeeper-stage3
```

## Check it is up

```sh
curl -sS http://127.0.0.1:8080/health
# {"status": "ok"}
```

## Use it

Open <http://127.0.0.1:8080/> for the booking site. The API is the same origin:

```sh
# seed a restaurant and two diners
curl -sS -X POST http://127.0.0.1:8080/_test/reset -H 'Content-Type: application/json' -d '{
  "users": [{"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}],
  "restaurants": [{"id": "r_anker", "name": "Zum Anker", "timezone": "Europe/Berlin",
    "slot_minutes": 30, "reservation_duration_minutes": 90,
    "cancellation_cutoff_minutes": 120,
    "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
    "tables": [{"id": "t_1", "label": "1", "capacity": 2},
               {"id": "t_2", "label": "2", "capacity": 4}],
    "combinable": [["t_1", "t_2"]]}],
  "reservations": []}'

# sign in
TOKEN=$(curl -sS -X POST http://127.0.0.1:8080/auth/login -H 'Content-Type: application/json' \
  -d '{"email": "ada@example.com", "password": "correct horse"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# book a table
curl -sS -X POST http://127.0.0.1:8080/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
       "starts_at_local": "2026-10-15T19:00", "party_size": 6}'
```

## Run without any outbound network

The grading environment has no route off the machine. This is the same command the
conformance harness uses:

```sh
docker run --rm -p 8080:8080 -e PORT=8080 --network none tablekeeper-stage3
```

## Notes

* State is in memory and is meant to be: `POST /_test/reset` replaces all of it, and
  `GET /_test/export` / `POST /_test/import` move it between processes.
* Startup is a few hundred milliseconds; the first healthy response depends only on
  the Python interpreter starting.

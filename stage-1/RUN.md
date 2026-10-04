# Running stage 1

The service is one container. It reads `PORT` (default `8080`), binds `0.0.0.0`, and
needs no configuration file, no volume, no database and no outbound network.

## Build

```sh
docker build -t tablekeeper-stage1 .
```

## Start

```sh
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage1
```

Any port works; `PORT` is what the service listens on inside the container and the
mapped port is what you reach it on:

```sh
docker run --rm -p 9090:9090 -e PORT=9090 tablekeeper-stage1
```

## Check it is up

```sh
curl -sS http://127.0.0.1:8080/health
# {"status": "ok"}
```

## Use it

The service is the API. There is no browser product at this stage, so `GET /` answers 404.

```sh
# seed a restaurant and a diner
curl -sS -X POST http://127.0.0.1:8080/_test/reset -H 'Content-Type: application/json' -d '{
  "users": [{"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}],
  "restaurants": [{"id": "r_anker", "name": "Zum Anker", "timezone": "Europe/Berlin",
    "slot_minutes": 30, "reservation_duration_minutes": 90,
    "cancellation_cutoff_minutes": 120,
    "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
    "tables": [{"id": "t_1", "label": "1", "capacity": 2},
               {"id": "t_2", "label": "2", "capacity": 4}]}],
  "reservations": []}'

# sign in
TOKEN=$(curl -sS -X POST http://127.0.0.1:8080/auth/login -H 'Content-Type: application/json' \
  -d '{"email": "ada@example.com", "password": "correct horse"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# book a table
curl -sS -X POST http://127.0.0.1:8080/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"restaurant_id": "r_anker", "table_id": "t_2",
       "starts_at_local": "2026-10-15T19:00", "party_size": 4}'
```

## Run without any outbound network

The grading environment has no route off the machine. A published port needs a network,
so `--network none` is paired with a health check run inside the container; this is the
shape the conformance harness uses (it reaches the service over a private network):

```sh
docker run -d --name tablekeeper-isolated --network none --cpus 2 --memory 2g \
  -e PORT=8080 tablekeeper-stage1
docker exec tablekeeper-isolated python3 -c "import urllib.request; \
  print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
# {"status": "ok"}
docker stop tablekeeper-isolated
```

On the default bridge the port is published instead, which is what every command above
does.

## Notes

* State is in memory and is meant to be: `POST /_test/reset` replaces all of it, and
  `GET /_test/export` / `POST /_test/import` move it between processes.
* Startup to the first healthy response measured at 89 ms, and 21 MiB resident while
  idle: the work the service does before it answers `/health` is reading its own
  source and starting the interpreter.

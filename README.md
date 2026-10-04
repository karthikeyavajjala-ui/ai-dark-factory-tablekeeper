# Tablekeeper

A restaurant booking service built as a dark factory: four seats work a specification in a
Band room, each stage's folder is the previous stage carried forward, and every claim in
this repository is backed by a command anyone can re-run.

**Track:** `tablekeeper` — reservations, availability, policies, recurring agreements and
closure replanning, delivered as a containerized HTTP service with a browser product from
stage 2 onwards.

## The band

| Seat | Mandate | Harness | Model | Owns |
|---|---|---|---|---|
| Foreman | [`mandates/foreman.md`](mandates/foreman.md) | Claude Code | claude-opus-4-1 | the plan, work items, acceptance decisions |
| Builder | [`mandates/builder.md`](mandates/builder.md) | Claude Code | claude-sonnet-4-5 | implementation, one work item at a time |
| Inspector | [`mandates/inspector.md`](mandates/inspector.md) | Codex | gpt-5-codex | independent verification and reproductions |
| Quartermaster | [`mandates/quartermaster.md`](mandates/quartermaster.md) | OpenCode | claude-sonnet-4-5 | image, run notes, copy-forward chain, reproducibility |

The `Harness:` and `Model:` lines in each mandate name the runtime that seat is configured
with in Band. If you stand the seats up with different runtimes, update those two lines so
the mandates keep describing the room that actually ran. See [`room/`](room) for the task
brief and the kickoff steps.

## How to read this repository

```
README.md                  this file: the band, the map, how to run everything
FACTORY.md                 how the factory works: seats, design choices, costs, failure handling
mandates/                  one mandate per seat — how each seat takes, does and hands on work
room/                      the task brief to paste into the room, and the kickoff steps
stage-1/ … stage-4/        the deliverable: four complete services, each buildable on its own
tools/build_stages.py      regenerates stages 1–3 from stage-4 (`--check` proves they match)
tools/official_suites.py   runs the shipped suites against a stage folder without Docker
tests/                     the repository's own conformance checks, written from the specs
```

Each `stage-N/` folder holds `Dockerfile`, `RUN.md` and the service source under `app/`.
They are not four programs: one service runs at four capability levels, and the level lives
in `stage-N/app/profile.py`. `tools/build_stages.py` rebuilds stages 1–3 from stage 4 by
copying and rewriting only that file (and dropping the browser assets from stage 1), so a
fix cannot drift between stages:

```sh
python3 tools/build_stages.py            # regenerate stages 1–3 from stage-4
python3 tools/build_stages.py --check    # prove they match apart from app/profile.py
```

## What each stage delivers

| Stage | Adds |
|---|---|
| 1 | JSON API: signup/login, public availability, bookings with idempotent retries, cancel/amend, atomic multi-booking moves, DST-correct local times, export/import |
| 2 | Browser product (search → grid → booking → confirmation → lookup) and combined-table bookings, with stale/lost-response recovery |
| 3 | Effective-dated policies, accepted-terms snapshots, revisions and history, availability explanations, recurring agreements |
| 4 | Closure replanning with deterministic minimisation, and recurring amendments that keep references and agreed terms |

## Running the service

Every stage builds and serves on its own; `stage-N/RUN.md` is the runbook a judge follows.

```sh
cd stage-4
docker build -t tablekeeper .
docker run --rm -p 8080:8080 --network none --cpus 2 --memory 2g tablekeeper
curl -s localhost:8080/health          # {"status":"ok"}
```

`--network none` is how it is graded: the image carries every runtime asset it needs, and
makes no outbound call. Without Docker, `cd stage-4 && PORT=8080 python3 -m app` runs the
same service. The reset/seed flow and a worked booking example are in each `RUN.md`.

## Running the checks

```sh
python3 -m pytest tests/ -q                 # the repository's own conformance checks
python3 tools/official_suites.py --stage 4 --repo .   # the shipped suites, per stage

# the official harness (from the kickoff package's directory)
python -m harness run --track tablekeeper --repo <this repo> --all --mode isolated --out runs/iso
python -m harness check <this repo> --track tablekeeper
```

`tests/` is written from the specification, not from the implementation: wire shapes,
idempotency (including concurrent retries), DST transitions in both directions, history
ordering, policy selection and adoption, series atomicity, and replan minimisation. It is
the regression suite for defects found while building, so each test names the rule it pins.

## Evidence

Measured on 2026-10-04, in this working tree:

| Check | Result |
|---|---|
| Repository checks | `97 passed in 10.92s` |
| Shipped suites, stage 4 folder | stage 1 `120 passed`, stage 2 `25 passed`, stage 3 `7 passed`, stage 4 `6 passed` |
| Official harness, `--stage 1 --mode isolated` | `stage 1: pass`, overshoot `stage 2: fail` (expected), `claimed stage: 1` |
| Official harness, `--all --mode isolated` | every folder claims its stage, `share 1.0`, `claimed stage: 4 on the shipped checks` |
| Container image | `127 MB` (`python:3.13-slim` + `tzdata`) |
| Start to first healthy response | `0.089 s` (limit: 60 s) |
| Idle resident memory | `21 MiB` (limit: 2 GiB) |
| 200 availability reads, 50 in flight | wall `0.14 s`, p95 `35 ms`, 0 errors |
| 50 concurrent creates, one key | `1 × 201`, `49 × 200`, one booking stored |

The full run logs are the harness's own `report.json` files; the numbers above are quoted
from them and from the commands in "Running the checks".

## Provenance, and what is still outstanding

The room log is what proves a band produced this code, and **there is no `room.json` in this
repository yet**. The factory definition is complete — mandates, seat ownership, the task
brief and the kickoff steps in `room/` — but the seats have not been run against it here,
because no Band room was available in the environment this repository was built in. Nothing
in this repository claims otherwise: the offline gate that reads the room will fail until
the room exists and its log is downloaded.

To finish the submission:

1. Create a Band room, add the four seats with the display names in the table above, and
   paste the brief from [`room/TASK.md`](room/TASK.md).
2. Let the band work the repository; the seats commit their own revisions as they go.
3. Download the room — Band console → the room's `⋮` menu → **Download → Download full
   session** — and save it **unchanged** as `room.json` at the root of this repository.
4. Re-run `python -m harness check <repo> --track tablekeeper`; it validates the folder
   layout, the mandates, the room log, the reciprocal `@handle` exchange and the
   credential scan.

Until step 3 lands, the repository is buildable, tested and self-consistent, and gate 2 of
the submission criteria is honestly reported as not met.

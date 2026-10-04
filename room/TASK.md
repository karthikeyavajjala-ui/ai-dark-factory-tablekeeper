# Task: tablekeeper

Paste this into the room when the four seats are joined. Everything a seat needs to know
about *this* problem is here, or in the specification this brief points at; the mandates in
`mandates/` say how each seat works.

## What we are building

A restaurant booking service — diners search availability, book a table (or two combined
tables), receive a reference, and can cancel or move bookings. Cutoffs, opening hours,
time zones and daylight-saving transitions are all part of the contract. It ships as a
containerized HTTP service. From stage 2 it also ships a browser product; stages 3 and 4
add dated booking policies with accepted terms, recurring agreements, and closure
replanning for a manager.

**Identity for the judges:** track `tablekeeper`. Each submittable stage is a complete,
buildable service in its own folder at the repository root, named `stage-1` through
`stage-4`, each carrying a `Dockerfile` and a `RUN.md`. A later folder is the earlier
folder carried forward and extended — never a fresh start — and every earlier
requirement still holds in it.

## The specification

The source of truth is the kickoff package's specification for this track:

- `tablekeeper/spec/stage-1.md` — reservations, availability, auth, idempotency, atomic
  multi-booking moves, export/import, time zone and DST rules
- `tablekeeper/spec/stage-2.md` — browser product, combined tables, uncertain outcomes
- `tablekeeper/spec/stage-3.md` — availability explanations, reservation history, dated
  policies with accepted terms and revisions, recurring agreements
- `tablekeeper/spec/stage-4.md` — closure replanning with deterministic minimisation, and
  recurring amendments

Read the specification, not this summary: every rule a judge checks is written there, and
the shipped suites in the package are only a sample. Our own checks live in `tests/`.

## Hard rules

1. **The shipped suites are read-only.** Never edit anything under the package's `test/`
   directories or the harness. Build to the specification; the judges hold the rest of the
   checks.
2. **The container is the deliverable.** A judge builds a stage folder's `Dockerfile` and
   follows its `RUN.md`; there is no manual setup step, no file outside the repository, and
   **no outbound network at run time**. Bundle every asset the interface needs.
3. **The runtime contract.** Listen on `0.0.0.0` and honour `PORT` (default `8080`);
   `GET /health` returns `{"status":"ok"}`; state is ephemeral and need not survive a
   restart. Stay inside 2 vCPU, 2 GiB, 60 s to first healthy response, 50 requests in
   flight, and never answer 5xx — including under concurrent load and concurrent retries.
4. **Errors have one shape** — `{"error":{"code":"…","message":"…"}}` — and each status and
   code in the specification is a contract, not a suggestion.
5. **No secrets, ever.** Nothing in the repository may hold a credential, a token or a key.
6. **Evidence, not claims.** Every work item is delivered as a committed revision plus the
   command that was run and its observed output.

## Definition of done for a stage

- The folder builds from a clean checkout and serves, following its `RUN.md` alone.
- The shipped suites for that stage and every earlier stage pass against it.
- Our own checks in `tests/` pass against it.
- The behaviour matches the specification's stated rules, including the DST transitions and
  the concurrency guarantees.
- The Foreman has accepted each work item on the Inspector's evidence, and the room's record
  shows which seat delivered what.

## Suggested starting split (the Foreman owns the real plan)

1. Stage 1 service skeleton: runtime contract, health, reset/seed, signup and login.
2. Stage 1 domain: availability grid, booking, cancel, amend — with cutoffs and DST.
3. Stage 1 delivery: idempotency and retries, atomic multi-booking moves, export/import.
4. Stage 2 browser product and combined tables, carried forward into `stage-2/`.
5. Stage 3 policies, accepted terms, revisions and history, then recurring agreements.
6. Stage 4 closure replanning and recurring amendments.
7. Packaging: each folder's `Dockerfile`, `RUN.md` and isolated-container verification.

Each numbered item is a work item: one seat, one committed revision, one command that
proves it, then review before the next starts.

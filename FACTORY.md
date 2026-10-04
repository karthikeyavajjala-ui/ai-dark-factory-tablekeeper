# Factory

## What this factory is

Four seats, one specification, and a rule that nothing counts until a command says so.
The Foreman splits the brief into scoped work items and decides acceptance; the Builder
implements one work item at a time and commits it; the Inspector attacks the result with
checks it writes itself; the Quartermaster owns the image, the run notes and the
copy-forward chain. Each seat has a mandate in [`mandates/`](mandates) that says how it
takes work, hands work on, and rejects it.

The seats are deliberately different runtimes — two Claude Code seats, a Codex seat and an
OpenCode seat — so verification is not the same model agreeing with itself.

## Seat ownership and setup

| Seat | Takes | Hands on | Rejects |
|---|---|---|---|
| Foreman | the brief | one work item per seat, with its acceptance condition | a revision with no evidence, or work that widens scope |
| Builder | one work item | committed revision + command + observed output | an acceptance condition it cannot test |
| Inspector | a delivered revision | a reproduction, or an acceptance with its checks | unreproducible evidence, happy-path-only checks |
| Quartermaster | a committed revision | build/startup/envelope numbers | a folder that needs something the notes do not mention |

Setting the room up: create a Band room, add four agent seats whose display names slug-match
the mandate filenames (`Foreman`, `Builder`, `Inspector`, `Quartermaster`), paste
[`room/TASK.md`](room/TASK.md) as the brief, and let the Foreman open work. The room is the
only channel: assignments, handoffs, rejections and acceptance all happen there, and a seat
that finds a defect posts it rather than reaching into another seat's files. `room/README.md`
has the step-by-step kickoff and the artifact to download at the end.

## The production line

One service, four capability levels. `stage-N/app/profile.py` is the only file that differs
between folders; everything else is copied forward. That decision came from a defect: early
on, the same fix had to be applied four times by hand, and one folder silently kept an old
bug. Now stages 1–3 are generated from stage 4, and a check fails if any file drifts:

```
python3 tools/build_stages.py           # regenerate stages 1–3 from stage-4
python3 tools/build_stages.py --check   # every file matches apart from app/profile.py
```

The chain rule is the same idea one level up: a folder counts only if every folder below it
builds and passes, so a break at stage 2 caps everything above it. That is why the generator
refuses to let the levels diverge, and why a stage gate is a capability flag rather than a
copy of the code.

## Design choices and why

- **Python standard library only.** No framework, no ORM, no external package at run time.
  The service is `ThreadingHTTPServer` + `zoneinfo` + `hashlib.scrypt`, which keeps the
  image at 127 MB, startup under a tenth of a second, and the "no outbound network at run
  time" rule trivially true rather than merely observed.
- **One lock, validation before commit.** Every write path validates completely, then
  mutates, under one re-entrant lock. Concurrency rules (one booking wins a race, a batch
  either fully applies or not at all) then fall out of the structure instead of being
  patched in per endpoint: 50 simultaneous creates on one key produce exactly one booking.
- **Idempotency receipts are first-class state.** A receipt stores the request body and the
  original response verbatim, so a replay is answered from the receipt, not re-derived, and
  it survives export/import. That is what makes "the replay still returns the original
  answer after the booking was cancelled" true rather than lucky.
- **Local time is resolved, never assumed.** Wall-clock input is resolved against the
  restaurant's zone: skipped times are rejected, ambiguous times take the first occurrence,
  and durations are added in absolute time, so a 90-minute booking that crosses a fall-back
  transition ends 90 real minutes later and reports the local time that implies.
- **Policies are snapshots, not references.** A booking stores the terms it accepted
  (the selected policy minus its effective date). Publishing a later policy cannot rewrite
  what an existing booking agreed to, and an amendment adopts the policy of the date it
  moves to and swaps terms and end time atomically.
- **A deterministic planner.** The closure planner enumerates seatings and minimises
  lexicographically: fewest bookings moved, then fewest unused seats, then lowest option
  ranks in reference order. Determinism is the deliverable — the same restaurant state
  always yields the same plan, so a manager can compare two previews and trust the diff.
- **The browser product is served by the same process.** Static files are read once at
  import and cached; the page issues no external request, in line with the runtime rules,
  and every screen is driven by the real API.

## How the factory catches a bad result

1. The Inspector writes checks from the specification and runs them against a revision it
   did not build; a verdict needs a reproduction.
2. Every defect found this way becomes a regression test in `tests/`, named after the rule
   it pins, so the same defect cannot come back unnoticed.
3. The shipped suites act as an external oracle against the same service, in both a plain
   process and an isolated container.
4. The Quartermaster rebuilds each folder the way a judge does — clean container, no
   network — and reports numbers instead of confidence.
5. The Quartermaster follows each folder's runbook literally, in a clean container, and runs
   the examples in it — a document that no longer matches the artifact is a defect.
6. The chain check (`build_stages.py --check`) and the offline submission check
   (`harness check`) catch the failures that are invisible in the working tree: stages that
   have drifted, a folder that is not self-contained, a mandate that names the problem
   instead of the factory, a credential in a file that is about to be published.

## What we tried that failed

- **Declared stage separation in code.** Four copies of the engine diverged within an hour;
  replaced by one engine plus the generator and its drift check.
- **Patching generated files with in-place regex edits.** A half-migrated file left one
  stage subtly wrong; whole files and whole functions are written instead.
- **Passing the store object where a policy mapping was expected.** Two endpoints returned
  500 under a policy state; the fix was one accessor used everywhere, plus a regression
  test for the create path.
- **Storing internal state in one shape and exporting it in another.** Reset and import
  both failed on the first write; the export boundary now serialises explicitly.
- **Leaking the policy's effective date into accepted terms.** The snapshot must be the
  terms, not the policy row; a conformance test compares the whole mapping.
- **Bumping an agreement's revision once per member in a batch.** Three members meant three
  bumps where the specification says one; batches now mark members and bump once.
- **Answering 401 where the specification says 404.** Two owner-only reads and the
  agreement read leak existence if they answer differently; all three now resolve the
  caller's identity and then refuse without confirming anything.
- **Normalising the caller's offset in an echoed interval.** A closure preview quoted the
  instants in UTC; it now echoes what the caller sent.
- **Timestamps with a fractional part.** Valid RFC 3339, but not the documented shape;
  every response timestamp is now whole seconds.
- **Assuming the tooling environment.** The browser checks needed system libraries that were
  not installed, and the Docker socket was not reachable as the current user; both were
  fixed in the environment rather than worked around in the repository.
- **A check that edited what it was checking.** The drift check compared each generated file
  by deleting it from the working tree, so a report run left three folders without the one
  file their container imports. A judge would have built an image that died at import.
  Found by running a folder's own runbook in a container, not by any passing suite; the fix
  compares the generated file against what the generator would write, and a regression test
  digests the whole tree before and after a report run.
- **A runbook that described a later stage.** The stage-1 runbook's worked example booked two
  combined tables — a stage-2 feature — so following it returned an error. The runbooks are
  generated too now, and the stage-1 variant is checked by running it in a clean container
  and asserting the example returns 201.

## Measured costs and time

Measured on 2026-10-04 in this tree (commands are in `README.md`):

| What | Number |
|---|---|
| Repository checks | 102 tests, ~11 s |
| Shipped suites on the stage-4 folder | 120 + 25 + 7 + 6 checks, ~45 s |
| Official harness, all folders, isolated containers | every folder claims its stage, `share 1.0` |
| Container image | 127 MB |
| Start to first healthy response | 0.089 s (limit 60 s) |
| Idle resident memory | 21 MiB (limit 2 GiB) |
| 200 availability reads, 50 in flight | 0.14 s wall, p95 35 ms, 0 errors |
| 50 concurrent creates on one key | 1 × 201, 49 × 200, one booking |
| Each folder's runbook followed in a clean container | stage 1 single-table booking `201`, stage 4 combined booking `201`, isolated health `200` |

Per-seat model time and token spend are not recorded here: the room has not been run yet,
and inventing those numbers would defeat the point of the log. When the band runs, its costs
belong in this table, taken from the room.

## Limits we know about

- **No room log yet.** The factory definition is complete, but no Band room was available
  where this repository was built, so `room.json` is absent and the room-dependent gate
  cannot pass. `README.md` states the exact steps to produce it.
- **One process keeps its state in memory.** Allowed by the specification (state need not
  survive a restart) and deliberately kept simple; export/import is the durable path.
- **The planner is exhaustive, not clever.** It is bounded by the documented planning limits
  (6 tables, 4 declared pairs, 6 considered bookings) and refuses larger inputs with the
  documented limit code rather than degrading.
- **No live notification when a plan is applied.** Diners see an applied repair the next
  time they read their booking; the specification does not ask for more.

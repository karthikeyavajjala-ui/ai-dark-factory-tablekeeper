# Room kit

What this folder is: the brief to paste into the band, and the steps that turn the room
into the one artifact the repository is still missing — `room.json`.

Everything else in the submission is already in this repository and verified by commands.
What is *not* here is the room log, and it cannot be written by hand: it has to come from a
real room. See "Download the room log" below.

## 1. Create the room

In Band Desktop, create a room named after the track. Add four agent seats, with these
**display names** — the offline check slug-matches each seat's display name against the
mandate filenames, so `Foreman` needs `mandates/foreman.md`, and so on:

| Display name | Mandate | Harness | Model |
|---|---|---|---|
| Foreman | `mandates/foreman.md` | Claude Code | claude-opus-4-1 |
| Builder | `mandates/builder.md` | Claude Code | claude-sonnet-4-5 |
| Inspector | `mandates/inspector.md` | Codex | gpt-5-codex |
| Quartermaster | `mandates/quartermaster.md` | OpenCode | claude-sonnet-4-5 |

If you configure a seat with a different harness or model, edit the `Harness:` and `Model:`
lines at the top of that seat's mandate to match what actually ran — those two lines are a
statement about the room, not decoration.

## 2. Give the band the work

Paste [`TASK.md`](TASK.md) into the room as the coordinator's message. It carries the
track's detail — which problem, which specification, which rules, which folders. Keep the
track's vocabulary in that message and out of the mandates: a mandate says how a seat works
and must read the same for any project.

Then work the loop: the Foreman opens a work item and assigns it; the Builder commits a
revision and posts the command it ran; the Inspector reproduces it independently and either
accepts or sends it back with a reproduction; the Quartermaster verifies the folder builds
and serves in isolation. The room is the only place a handoff happens.

**Gate 2 needs a two-way exchange in the room:** at least two of your own seats must address
each other with their `@handle` and each must reply — a mention in one direction is not
enough. Half of the judging is whether the seats shared the work, so let the Foreman
assign, let a seat push back with evidence, and let the work return through the room.

## 3. Commit what the band produced

Push the history the seats made, without amending, rebasing or squashing it. Anything you
commit under `stage-N/` yourself is code the band did not write, and the commit history is
read next to the room log.

## 4. Download the room log

1. Open the room in Band Desktop, use the room's `⋮` menu and choose **Open in Band**.
2. In the console, open the room's `⋮` menu and choose **Download → Download full session**.
   Not **Download filtered** — a filtered download fails the check.
3. Save the file **unchanged** as `room.json` at the root of this repository. Do not edit it.

Download it at the end, once the work is done, so the log holds the whole collaboration.
Read it before committing: the repository is public, and while the check scans for credential
shapes, it cannot recognise every private value. If one is in there, rotate the credential
and replace the value with `[REDACTED]`.

## 5. Check and submit

```sh
python -m harness check <this repo> --track tablekeeper     # layout, mandates, room, secrets
python -m harness run --track tablekeeper --repo <this repo> --all --mode isolated --out runs/final
```

`harness check` builds nothing; it is the offline proof of gates 1, 2 and the mandate part
of gate 4. `harness run --all --mode isolated` builds every folder and runs the shipped
suites inside containers, with no outbound network — that is gate 3, and the closest thing
to the way a judge will read this repository.

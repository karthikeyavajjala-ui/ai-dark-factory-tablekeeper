#!/usr/bin/env python3
"""Produce stage-1, stage-2 and stage-3 from stage-4.

Every stage folder is a complete, independently buildable service. They are the same
service at four points in its life, so rather than maintain four hand-edited copies of
the same engine, `stage-4/` is the source of truth and this script copies it backwards,
rewriting exactly one file: `app/profile.py`.

What differs between the folders is therefore exactly what the spec says differs --
which surfaces are routed, which fields a request may carry, and which response fields
appear -- and it is reviewable in a single diff per stage instead of four. The stage-1
folder additionally drops the browser assets, because stage 1 has no browser product
and shipping one would be a later answer filed in an earlier folder.

    python3 tools/build_stages.py            # regenerate stages 1-3
    python3 tools/build_stages.py --check    # verify they are current; change nothing
"""
from __future__ import annotations

import argparse
import filecmp
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = "stage-4"
STAGE1_DROPS = ("app/frontend.py", "app/static")

PROFILE = '''"""Which stage of the service this folder is.

Every stage folder ships the same domain engine. What separates them is the profile:
which surfaces are routed, which fields a request may carry, and which response fields
appear. Keeping the difference in one small file makes the folders diffable against
each other, and makes "this folder answers this stage and not the next one" a property
of the profile rather than of a hand-edited copy.
"""
STAGE = {stage}

# Derived capabilities. Named here so the engine reads as intent, not as arithmetic.
COMBINATIONS = STAGE >= 2     # declared pairs, available_options, the browser product
UI = STAGE >= 2               # the four browser routes
POLICIES = STAGE >= 3         # dated policies, accepted terms, explain, revisions
HISTORY = STAGE >= 3          # reservation history and history-reading endpoints
SERIES = STAGE >= 3           # recurring agreements
REPLAN = STAGE >= 4           # closure preview and application, series amendment
'''

# The profile is the one file a stage folder is *meant* to have its own copy of.
IGNORED = {"app/profile.py"}


def STAGE1_RUNBOOK(text: str) -> str:
    """A runbook that describes stage 1, not a later stage with a caveat.

    Stage 1 has no browser product and one table per booking, so the example has to
    book a single table: a reader following these commands must see a 201, not an error
    about a field stage 1 never had.
    """
    text = text.replace("Open <http://127.0.0.1:8080/> for the booking site. The API is "
                        "the same origin:",
                        "The service is the API. There is no browser product at this "
                        "stage, so `GET /` answers 404.")
    text = text.replace("""# seed a restaurant and two diners""",
                        """# seed a restaurant and a diner""")
    text = text.replace("""               {"id": "t_2", "label": "2", "capacity": 4}],
    "combinable": [["t_1", "t_2"]]}],""",
                        """               {"id": "t_2", "label": "2", "capacity": 4}]}],""")
    text = text.replace("""  -d '{"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
       "starts_at_local": "2026-10-15T19:00", "party_size": 6}'""",
                        """  -d '{"restaurant_id": "r_anker", "table_id": "t_2",
       "starts_at_local": "2026-10-15T19:00", "party_size": 4}'""")
    return text


def materialise(stage: int, root: pathlib.Path) -> pathlib.Path:
    """Write the stage folder `stage` under `root` and return it."""
    target = root / f"stage-{stage}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(root / SOURCE, target)
    (target / "Dockerfile").write_text(
        (root / SOURCE / "Dockerfile").read_text().replace(
            "# TableKeeper -- stage 4.", f"# TableKeeper -- stage {stage}."))
    runbook = ((root / SOURCE / "RUN.md").read_text()
               .replace("stage 4", f"stage {stage}")
               .replace("tablekeeper-stage4", f"tablekeeper-stage{stage}")
               .replace("stage4", f"stage{stage}"))
    if stage == 1:
        runbook = STAGE1_RUNBOOK(runbook)
    (target / "RUN.md").write_text(runbook)
    (target / "app" / "profile.py").write_text(PROFILE.format(stage=stage))
    if stage == 1:
        for relative in STAGE1_DROPS:
            path = target / relative
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return target


# Compared by content above, then ignored by the directory walk: `app/profile.py` is
# the one file a stage folder is *meant* to differ in.
IGNORE_IN_WALK = ["__pycache__", "profile.py"]


def differences(a: pathlib.Path, b: pathlib.Path, prefix="") -> list:
    out = []
    comparison = filecmp.dircmp(a, b, ignore=list(IGNORE_IN_WALK))
    for name in comparison.left_only:
        out.append(f"{prefix}{name} is only in {a}")
    for name in comparison.right_only:
        out.append(f"{prefix}{name} is only in {b}")
    for name in comparison.diff_files:
        out.append(f"{prefix}{name} differs")
    for name, child in comparison.subdirs.items():
        out.extend(differences(pathlib.Path(child.left), pathlib.Path(child.right),
                               f"{prefix}{name}/"))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report whether stages 1-3 match stage-4; change nothing")
    args = parser.parse_args(argv)

    if not args.check:
        for stage in (1, 2, 3):
            target = materialise(stage, ROOT)
            print(f"built {target.relative_to(ROOT)}")
        return 0

    with tempfile.TemporaryDirectory() as scratch:
        scratch = pathlib.Path(scratch)
        shutil.copytree(ROOT / SOURCE, scratch / SOURCE)   # the shared source
        problems = []
        for stage in (1, 2, 3):
            expected = materialise(stage, scratch)
            current = ROOT / f"stage-{stage}"
            if not current.is_dir():
                problems.append(f"stage-{stage}/ is missing")
                continue
            for relative in sorted(IGNORED):
                # Compare the generated files against what this generator would write,
                # and drop them from the *scratch* copy only: `--check` promises to
                # change nothing, and an earlier revision of this loop deleted the
                # working tree's profile.py -- which the container then could not import.
                want = (expected / relative).read_bytes()
                here = (current / relative).read_bytes() \
                    if (current / relative).is_file() else b""
                if want != here:
                    problems.append(f"stage-{stage}/{relative} is out of date")
                (expected / relative).unlink(missing_ok=True)
            problems.extend(differences(current, expected))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("stages 1-3 match stage-4 apart from app/profile.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

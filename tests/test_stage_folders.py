"""The folders themselves: complete, reproducible, and unmoved by a check.

`tools/build_stages.py` generates stages 1-3 from stage 4, so the repository keeps one
copy of the engine. These checks pin the two ways that arrangement can fail: a folder
that is missing a piece a judge needs, and a generator that edits the working tree when
it was asked only to report.
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
STAGES = (1, 2, 3, 4)
REQUIRED = ("Dockerfile", "RUN.md", ".dockerignore", "app/__init__.py", "app/__main__.py",
            "app/engine.py", "app/profile.py", "app/server.py", "app/store.py",
            "app/timeutil.py", "app/util.py")


def tree_digest(root: pathlib.Path) -> str:
    """A hash of every generated file, so a mutation shows up as a different digest."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or ".git" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_generator(*args):
    return subprocess.run([sys.executable, "tools/build_stages.py", *args], cwd=REPO,
                          capture_output=True, text=True, timeout=300)


def test_every_stage_folder_is_complete():
    for stage in STAGES:
        folder = REPO / f"stage-{stage}"
        assert folder.is_dir(), f"{folder.name}/ is missing"
        for relative in REQUIRED:
            assert (folder / relative).is_file(), f"{folder.name}/{relative} is missing"
        assert (folder / "app" / "static" / "app.js").is_file() == (stage >= 2)
        assert (folder / "app" / "frontend.py").is_file() == (stage >= 2)


def test_the_profiles_say_which_stage_they_are():
    for stage in STAGES:
        text = (REPO / f"stage-{stage}" / "app" / "profile.py").read_text()
        assert f"STAGE = {stage}\n" in text, f"stage-{stage} does not declare itself"


def test_the_generator_check_reports_and_changes_nothing():
    before = {stage: tree_digest(REPO / f"stage-{stage}") for stage in STAGES}
    result = run_generator("--check")
    assert result.returncode == 0, result.stderr or result.stdout
    after = {stage: tree_digest(REPO / f"stage-{stage}") for stage in STAGES}
    assert before == after, "`--check` modified the working tree; it must only report"


def test_a_folder_carried_forward_does_not_keep_its_own_git():
    for stage in STAGES:
        assert not (REPO / f"stage-{stage}" / ".git").exists(), \
            f"stage-{stage}/.git would become a gitlink and clone as an empty folder"


def test_the_python_in_every_folder_compiles():
    for stage in STAGES:
        result = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(REPO / f"stage-{stage}" / "app")],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, f"stage-{stage}: {result.stdout}{result.stderr}"
        for cache in (REPO / f"stage-{stage}").rglob("__pycache__"):
            for item in sorted(cache.rglob("*"), reverse=True):
                item.unlink() if item.is_file() else item.rmdir()
            cache.rmdir()

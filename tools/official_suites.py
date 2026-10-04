#!/usr/bin/env python3
"""Run the event's shipped suites against a stage folder, without Docker.

`python -m harness run --mode isolated` is what decides a submission, and it is what
`RUN.md` is for. This script exists because a development machine may not have a
Docker daemon: it starts the stage folder with `python -m app` on a free port and runs
exactly the same pytest suites the harness would, with the same plugin, the same
options and the same per-stage chain. It also runs the harness's *overshoot* probe --
the next stage's suite -- because a stage folder that passes the following suite is a
later answer filed in the wrong place and claims nothing.

    python3 tools/official_suites.py --stage 4 --repo .
    python3 tools/official_suites.py --all --repo .

Set DF_KICKOFF if the kickoff package is not at ../.official/dark-factory-wearedevs.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_KICKOFF = HERE.parent / ".official" / "dark-factory-wearedevs"
STAGES = ("1", "2", "3", "4")


def kickoff_root() -> pathlib.Path:
    override = os.environ.get("DF_KICKOFF")
    candidates = [pathlib.Path(override)] if override else []
    candidates.append(DEFAULT_KICKOFF)
    for candidate in candidates:
        if (candidate / "harness").is_dir() and (candidate / "tablekeeper").is_dir():
            return candidate
    raise SystemExit("cannot find the kickoff package; set DF_KICKOFF to its path")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_healthy(base_url: str, process: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"service exited before becoming healthy (rc={process.returncode})")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.2)
    raise SystemExit(f"service did not become healthy at {base_url}")


class Service:
    """A stage folder, running."""

    def __init__(self, folder: pathlib.Path):
        self.folder = folder
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process = None

    def __enter__(self) -> "Service":
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["PYTHONPATH"] = str(self.folder)
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "app"], cwd=str(self.folder), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_healthy(self.base_url, self.process)
        return self

    def __exit__(self, *exc):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        return False


def run_suite(kickoff: pathlib.Path, track: str, stage: str, base_url: str,
              previous: str | None, extra: list) -> tuple:
    """Run one stage's suite. Returns (passed, summary line)."""
    args = [sys.executable, "-m", "pytest",
            str(kickoff / track / "test" / f"stage_{stage}"),
            "-p", "harness.plugin",
            "--base-url", base_url,
            "--rootdir", str(kickoff / track / "test"),
            "-q", *extra]
    if previous:
        args += ["--previous-base-url", previous]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(kickoff)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["COLUMNS"] = "200"
    proc = subprocess.run(args, capture_output=True, text=True, env=env, cwd=str(kickoff))
    tail = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    summary = tail[-1] if tail else "(no output)"
    line = f"  stage {stage}: {'pass' if proc.returncode == 0 else 'fail'}  ({summary})"
    if proc.returncode != 0:
        failures = [line for line in tail if line.startswith("FAILED") or line.startswith("ERROR")]
        line += "\n" + "\n".join(f"      {item[:200]}" for item in failures[:12])
    return proc.returncode == 0, line


def check_stage(kickoff: pathlib.Path, track: str, repo: pathlib.Path, stage: str,
                verbose: bool) -> bool:
    folder = repo / f"stage-{stage}"
    if not folder.is_dir():
        print(f"  stage {stage}: missing {folder}")
        return False
    results = []
    with Service(folder) as service:
        previous_url = None
        with_previous = None
        if stage != "1":
            with_previous = Service(repo / f"stage-{int(stage) - 1}")
            with_previous.__enter__()
            previous_url = with_previous.base_url
        try:
            for earlier in STAGES:
                if earlier > stage:
                    break
                passed, line = run_suite(kickoff, track, earlier, service.base_url,
                                         previous_url if earlier != "1" else None, [])
                results.append((earlier, passed))
                print(line)
        finally:
            if with_previous:
                with_previous.__exit__(None, None, None)
        # The overshoot probe: a stage folder must NOT pass the next stage's suite.
        following = str(int(stage) + 1)
        if following in STAGES and (kickoff / track / "test" / f"stage_{following}").is_dir():
            passed, line = run_suite(kickoff, track, following, service.base_url, None, ["-x"])
            print(f"  probe stage {following}: {'fail' if not passed else 'PASS'}"
                  f"  (expected fail)")
            if passed:
                print(f"  stage-{stage}/ also passes the stage {following} suite, "
                      f"so it is not a stage {stage} solution")
                return False
            if verbose:
                print(f"      {line.splitlines()[0][:160]}")
    return all(passed for _, passed in results)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", default="tablekeeper")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    kickoff = kickoff_root()
    repo = pathlib.Path(args.repo).resolve()
    stages = list(STAGES) if args.all else [args.stage] if args.stage else ["1"]
    print(f"shipped suites from {kickoff}  ·  repo {repo}")
    ok = True
    for stage in stages:
        print(f"stage-{stage}/ ...")
        ok = check_stage(kickoff, args.track, repo, stage, args.verbose) and ok
    print("highest contiguous stage:",
          max([int(s) for s in stages if ok] or [0]))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared plumbing: start the service from source and talk HTTP to it.

The service is a container image in the submission, but it is a plain `python -m app`
process too, so the repository's own checks run without Docker. Anything the tests
observe here is the same HTTP surface the harness observes.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
STAGE = os.environ.get("TK_STAGE", "stage-4")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Client:
    """A tiny HTTP client: enough to assert on status, body and raw text."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def request(self, method, path, body=None, token=None, key=None, raw=None,
                content_type="application/json"):
        url = self.base_url + path
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        headers = {}
        if data is not None:
            headers["Content-Type"] = content_type
        if token:
            headers["Authorization"] = "Bearer " + token
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return Reply(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as exc:
            return Reply(exc.code, exc.read(), dict(exc.headers))

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def patch(self, path, **kw):
        return self.request("PATCH", path, **kw)

    # -- assertions used all over the suite -------------------------------------
    def ok(self, method, path, status=200, **kw):
        reply = self.request(method, path, **kw)
        assert reply.status == status, f"{method} {path} -> {reply.status} {reply.text}"
        return reply.json()

    def fails(self, method, path, status, code, **kw):
        reply = self.request(method, path, **kw)
        assert reply.status == status, \
            f"{method} {path}: wanted {status}, got {reply.status} {reply.text}"
        body = reply.json()
        assert body["error"]["code"] == code, \
            f"{method} {path}: wanted {code}, got {body['error']['code']}"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]
        return body


class Reply:
    def __init__(self, status: int, raw: bytes, headers: dict):
        self.status = status
        self.raw = raw
        self.headers = headers

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.raw)


@pytest.fixture(scope="session")
def service():
    port = free_port()
    env = dict(os.environ, PORT=str(port), PYTHONUNBUFFERED="1")
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "app"], cwd=REPO / STAGE, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    client = Client(base)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"service exited:\n{process.stdout.read()}")
            try:
                reply = client.get("/health")
                if reply.status == 200 and reply.json() == {"status": "ok"}:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("service never became healthy")
        yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def client(service):
    return service

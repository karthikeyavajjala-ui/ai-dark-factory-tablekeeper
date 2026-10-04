"""Small in-memory task tracker HTTP server and JSON API."""

from __future__ import annotations

import json
import mimetypes
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4


MAX_TITLE_LENGTH = 200
WEB_ROOT = Path(__file__).resolve().parent


class TaskStore:
    """Thread-safe, in-memory task collection."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            return [task.copy() for task in self._tasks.values()]

    def create(self, title: str) -> dict[str, object]:
        task = {
            "id": str(uuid4()),
            "title": title,
            "completed": False,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        with self._lock:
            self._tasks[task["id"]] = task
        return task.copy()

    def update(self, task_id: str, completed: bool) -> dict[str, object] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task["completed"] = completed
            return task.copy()

    def delete(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(task_id, None) is not None


def validate_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    title = value.strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        return None
    return title


class TaskRequestHandler(BaseHTTPRequestHandler):
    store = TaskStore()
    server_version = "TaskTracker/1.0"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/tasks":
            self._send_json(200, {"tasks": self.store.list()})
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/tasks":
            self._send_error_json(404, "Not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        title = validate_title(payload.get("title")) if isinstance(payload, dict) else None
        if title is None:
            self._send_error_json(400, "title must be a non-empty string of at most 200 characters")
            return
        self._send_json(201, {"task": self.store.create(title)})

    def do_PATCH(self) -> None:  # noqa: N802
        task_id = self._task_id()
        if task_id is None:
            self._send_error_json(404, "Not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("completed"), bool):
            self._send_error_json(400, "completed must be a boolean")
            return
        task = self.store.update(task_id, payload["completed"])
        if task is None:
            self._send_error_json(404, "Task not found")
            return
        self._send_json(200, {"task": task})

    def do_DELETE(self) -> None:  # noqa: N802
        task_id = self._task_id()
        if task_id is None:
            self._send_error_json(404, "Not found")
            return
        if not self.store.delete(task_id):
            self._send_error_json(404, "Task not found")
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _task_id(self) -> str | None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "tasks"] and parts[2]:
            return parts[2]
        return None

    def _read_json(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._send_error_json(400, "Request body must be valid JSON")
            return None
        return payload if isinstance(payload, dict) else {}

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            relative = "web/index.html" if (WEB_ROOT / "web" / "index.html").is_file() else "index.html"
        else:
            relative = path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if not target.is_relative_to(WEB_ROOT) or not target.is_file():
            self._send_error_json(404, "Not found")
            return
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, status: int, payload: object) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), TaskRequestHandler)


if __name__ == "__main__":
    host = os.environ.get("TASK_HOST", "127.0.0.1")
    port = int(os.environ.get("TASK_PORT", "8000"))
    server = create_server(host, port)
    print(f"Task tracker listening at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

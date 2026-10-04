"""The HTTP surface.

One handler, one route table, one place where a bearer token becomes an account. The
spec fixes the boundaries, so they are written down here rather than spread through
the engine:

* public: `/health`, the three test-control endpoints, the two auth endpoints, and
  the three browsing endpoints diners reach before they sign in;
* everything else needs a bearer token -- except reservation history and decision,
  which answer 404 rather than 401, because those two must not reveal that a
  reference exists.
"""
from __future__ import annotations

import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import profile
from .engine import Engine
from .store import Store
from .util import ApiError, not_found

JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
MAX_BODY_BYTES = 8 * 1024 * 1024

# Test-control calls get a longer client-side timeout than ordinary requests, but the
# service itself never delays a response.
PUBLIC_EXACT = {"/health", "/_test/reset", "/_test/export", "/_test/import",
                "/auth/signup", "/auth/login"}
OWNER_404 = ("history", "decision")   # 404 rather than 401 when unauthenticated


def _load_static():
    """Read the browser assets once, at import, so the image needs no file lookups."""
    here = os.path.dirname(os.path.abspath(__file__))
    assets = {}
    if profile.UI:
        from . import frontend
        assets = frontend.ASSETS
    return here, assets


def _segments(path: str) -> list:
    return [unquote(part) for part in path.split("/") if part]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TableKeeper"
    sys_version = ""

    # ---- plumbing --------------------------------------------------------

    def log_message(self, *args):
        """Quiet: the container's stdout is for operational lines, not access logs."""

    def _read_body(self):
        """`(parsed, ok)`. `ok` False means the body did not parse as a JSON object."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None, False
        if length <= 0:
            return {}, True
        if length > MAX_BODY_BYTES:
            return None, False
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, False
        return parsed, isinstance(parsed, dict)

    def _send(self, status: int, payload, content_type=JSON_TYPE):
        if status == 204 or payload is None:
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, error: ApiError):
        self._send(error.status, error.body())

    def _token(self):
        header = self.headers.get("Authorization") or ""
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            return None
        return parts[1].strip()

    # ---- methods ---------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PATCH, OPTIONS")
        self.end_headers()

    # ---- dispatch --------------------------------------------------------

    def _route(self, method: str):
        split = urlsplit(self.path)
        path = split.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        query = {key: values[0] for key, values in parse_qs(split.query,
                                                            keep_blank_values=True).items()}
        body, parsed_ok = self._read_body()
        try:
            status, payload, content_type = self._dispatch(method, path, query, body,
                                                           parsed_ok)
        except ApiError as error:
            self._fail(error)
            return
        except Exception:                       # never leak a 5xx and never crash
            import traceback
            traceback.print_exc()
            self._fail(ApiError(500, "internal_error", "an unexpected error occurred"))
            return
        self._send(status, payload, content_type)

    def _dispatch(self, method, path, query, body, parsed_ok):
        segments = _segments(path)
        engine = self.server.engine

        # -- the browser product ------------------------------------------
        if profile.UI and method == "GET" and segments and segments[0] == "assets":
            name = "/".join(segments[1:])
            asset = self.server.assets.get(name)
            if asset is None:
                raise not_found("no such asset")
            return 200, asset[0], asset[1]
        if profile.UI and method == "GET" and (not segments or segments[0] in
                                              ("signup", "login", "lookup")):
            from . import frontend
            return 200, frontend.page(), HTML_TYPE

        # -- health ---------------------------------------------------------
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}, JSON_TYPE

        # -- test control ---------------------------------------------------
        if path in ("/_test/reset", "/_test/import") and method == "POST":
            if not parsed_ok:
                raise ApiError(400, "malformed_request",
                               "the request body must be a JSON object")
            if path == "/_test/reset":
                return engine.reset(body)[0], None, JSON_TYPE
            return engine.import_state(body)[0], None, JSON_TYPE
        if method == "GET" and path == "/_test/export":
            status, payload = engine.export_state()
            return status, payload, JSON_TYPE

        # -- auth -----------------------------------------------------------
        if method == "POST" and path == "/auth/signup":
            if not parsed_ok:
                raise ApiError(400, "malformed_request",
                               "the request body must be a JSON object")
            status, payload = engine.signup(body)
            return status, payload, JSON_TYPE
        if method == "POST" and path == "/auth/login":
            if not parsed_ok:
                raise ApiError(400, "malformed_request",
                               "the request body must be a JSON object")
            status, payload = engine.login(body)
            return status, payload, JSON_TYPE

        # -- the ones that answer 404 instead of 401 ------------------------
        # A caller who is not the owner is told nothing, and a caller with no token is
        # treated the same way: the existence of someone else's booking or agreement is
        # not something this service confirms.
        if method == "GET" and len(segments) == 3 and segments[0] == "reservations" \
                and segments[2] in OWNER_404:
            user = engine.user_for_token(self._token())
            if user is None:
                raise not_found("no such reservation")
            if segments[2] == "history":
                return engine.reservation_history(segments[1], user["id"]) + (JSON_TYPE,)
            return engine.reservation_decision(segments[1], user["id"]) + (JSON_TYPE,)
        if method == "GET" and len(segments) == 2 and segments[0] == "series":
            user = engine.user_for_token(self._token())
            if user is None:
                raise not_found("no such series")
            return engine.get_series(segments[1], user["id"]) + (JSON_TYPE,)

        # -- everything else: authentication, then routing ------------------
        public = (method == "GET" and (
            path == "/restaurants"
            or (len(segments) == 2 and segments[0] == "restaurants")
            or (profile.POLICIES and len(segments) == 3 and segments[0] == "restaurants"
                and segments[2] == "policies")
            or path == "/availability"))
        token = self._token()
        user = engine.user_for_token(token)
        if not public and user is None:
            raise ApiError(401, "unauthenticated",
                           "a bearer token is required")

        user_id = user["id"] if user else None
        key = self.headers.get("Idempotency-Key")

        if method == "GET" and path == "/restaurants":
            return engine.list_restaurants() + (JSON_TYPE,)
        if method == "GET" and len(segments) == 2 and segments[0] == "restaurants":
            return engine.restaurant_detail(segments[1]) + (JSON_TYPE,)
        if method == "GET" and path == "/availability":
            return engine.availability(query) + (JSON_TYPE,)

        if profile.POLICIES:
            if (method == "GET" and len(segments) == 3 and segments[0] == "restaurants"
                    and segments[2] == "policies"):
                return engine.list_policies(segments[1]) + (JSON_TYPE,)
            if (method == "POST" and len(segments) == 3 and segments[0] == "restaurants"
                    and segments[2] == "policies"):
                if not parsed_ok:
                    raise ApiError(400, "malformed_request",
                                   "the request body must be a JSON object")
                return engine.publish_policy(segments[1], body, user_id, key) + (JSON_TYPE,)

        if profile.REPLAN:
            if (method == "POST" and len(segments) == 3 and segments[0] == "restaurants"
                    and segments[2] == "replans"):
                if not parsed_ok:
                    raise ApiError(400, "malformed_request",
                                   "the request body must be a JSON object")
                return engine.create_replan(segments[1], body, user_id, key) + (JSON_TYPE,)
            if (method == "POST" and len(segments) == 5 and segments[0] == "restaurants"
                    and segments[2] == "replans" and segments[4] == "apply"):
                if not parsed_ok:
                    raise ApiError(400, "malformed_request",
                                   "the request body must be a JSON object")
                return engine.apply_replan(segments[1], segments[3], body, user_id,
                                           key) + (JSON_TYPE,)

        if method == "POST" and path == "/reservations":
            if not parsed_ok:
                raise ApiError(400, "malformed_request",
                               "the request body must be a JSON object")
            return engine.create_reservation(body, user_id, key) + (JSON_TYPE,)
        if method == "GET" and path == "/reservations":
            return engine.list_reservations(user_id) + (JSON_TYPE,)
        if method == "POST" and path == "/reservation-moves":
            if not parsed_ok:
                raise ApiError(400, "malformed_request",
                               "the request body must be a JSON object")
            return engine.reservation_moves(body, user_id, key) + (JSON_TYPE,)

        if profile.SERIES:
            if method == "POST" and path == "/series":
                if not parsed_ok:
                    raise ApiError(400, "malformed_request",
                                   "the request body must be a JSON object")
                return engine.create_series(body, user_id, key) + (JSON_TYPE,)
            if method == "GET" and len(segments) == 2 and segments[0] == "series":
                return engine.get_series(segments[1], user_id) + (JSON_TYPE,)
            if (profile.REPLAN and method == "POST" and len(segments) == 3
                    and segments[0] == "series" and segments[2] == "amend"):
                if not parsed_ok:
                    raise ApiError(400, "malformed_request",
                                   "the request body must be a JSON object")
                return engine.amend_series(segments[1], body, user_id, key) + (JSON_TYPE,)

        if len(segments) == 2 and segments[0] == "reservations":
            reference = segments[1]
            if method == "GET":
                return engine.get_reservation(reference, user_id) + (JSON_TYPE,)
            if method == "PATCH":
                if not parsed_ok:
                    raise ApiError(400, "malformed_request",
                                   "the request body must be a JSON object")
                return engine.patch_reservation(reference, body, user_id) + (JSON_TYPE,)
        if (method == "POST" and len(segments) == 3 and segments[0] == "reservations"
                and segments[2] == "cancel"):
            return engine.cancel_reservation(segments[1], user_id) + (JSON_TYPE,)

        raise not_found("no such endpoint")


class Server(ThreadingHTTPServer):
    # The spec allows 50 requests in flight. The stdlib default backlog of 5 drops
    # connections well below that, which shows up as phantom failures under load.
    request_queue_size = 512
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, engine, assets):
        self.engine = engine
        self.assets = assets
        super().__init__(address, handler)

    def handle_error(self, request, client_address):
        """A dropped connection is not worth a traceback on stdout."""
        if isinstance(__import__("sys").exc_info()[1], (BrokenPipeError,
                                                        ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve(port: int | None = None, host: str = "0.0.0.0") -> None:
    port = int(port if port is not None else os.environ.get("PORT", "8080"))
    engine = Engine(Store())
    _here, assets = _load_static()
    assets = _asset_table(assets)
    server = Server((host, port), Handler, engine, assets)
    print(f"tablekeeper stage {profile.STAGE} listening on {host}:{port}", flush=True)
    server.serve_forever()


def _asset_table(assets: dict) -> dict:
    table = {}
    for name, payload in assets.items():
        if name.endswith(".css"):
            table[name] = (payload.encode("utf-8"), "text/css; charset=utf-8")
        elif name.endswith(".js"):
            table[name] = (payload.encode("utf-8"), "application/javascript; charset=utf-8")
        else:
            table[name] = (payload.encode("utf-8"), "text/plain; charset=utf-8")
    return table


def _assets_from_frontend() -> dict:
    if not profile.UI:
        return {}
    from . import frontend
    return dict(frontend.ASSETS)

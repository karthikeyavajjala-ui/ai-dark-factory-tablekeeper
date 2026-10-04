"""The browser assets, read once at import.

They are files, not CDN links: the container has no outbound network at run time, so
every stylesheet, script and icon has to be inside the image. Reading them here means
the service does no disk work per request.
"""
from __future__ import annotations

import pathlib

_STATIC = pathlib.Path(__file__).resolve().parent / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


ASSETS = {
    "app.css": _read("app.css"),
    "app.js": _read("app.js"),
}

_PAGE_BYTES = _read("index.html").encode("utf-8")


def page() -> bytes:
    """The single-page shell the four browser routes are answered with."""
    return _PAGE_BYTES

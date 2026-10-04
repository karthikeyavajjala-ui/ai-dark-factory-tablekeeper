"""Shared primitives: the error type every refusal uses, small validators, ids."""
from __future__ import annotations

import json
import re
import secrets

# IDs are opaque strings of at most 64 characters (spec §3.4).
MAX_ID_LENGTH = 64
# References are 6..12 characters of A-Z0-9 and never change (spec §8).
REFERENCE_RE = re.compile(r"^[A-Z0-9]{6,12}$")
REFERENCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ApiError(Exception):
    """A refusal carrying the HTTP status and the `error.code` to answer with."""

    def __init__(self, status: int, code: str, message: str | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def malformed(message: str = "the request body could not be read") -> ApiError:
    """400 -- unparseable body, or a field of the wrong JSON type."""
    return ApiError(400, "malformed_request", message)


def invalid(message: str = "a stated rule was violated") -> ApiError:
    """422 -- a required field or query parameter is missing, or out of range."""
    return ApiError(422, "validation_failed", message)


def not_found(message: str = "no such resource") -> ApiError:
    return ApiError(404, "not_found", message)


def new_reference() -> str:
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def is_int(value) -> bool:
    """A JSON integer. `bool` is a subclass of `int` in Python and is never one."""
    return isinstance(value, int) and not isinstance(value, bool)


def plain_decimal_digits(text) -> int | None:
    """A query integer written as plain decimal digits, or None.

    `1e9`, `4.0` and `+4` are plain 422 whatever their numeric value, so this is a
    lexical check rather than a conversion.
    """
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]+", text):
        return None
    return int(text)


def check_id(value, what: str) -> str:
    """An opaque id: a string of at most 64 characters."""
    if not isinstance(value, str):
        raise malformed(f"{what} must be a string")
    if not value or len(value) > MAX_ID_LENGTH:
        raise invalid(f"{what} must be 1..{MAX_ID_LENGTH} characters")
    return value


def jsonable(value):
    """Round-trip a value through JSON so stored receipts compare as JSON values."""
    return json.loads(json.dumps(value))

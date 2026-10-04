"""TableKeeper -- an HTTP service for restaurant reservations.

The package is the whole service: `engine` holds the behaviour, `server` is the HTTP
surface, `store` owns the state. Run it with `python -m app`.
"""
from .profile import STAGE

__all__ = ["STAGE"]

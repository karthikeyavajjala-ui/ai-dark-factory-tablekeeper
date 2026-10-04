"""`python -m app` -- the container's entry point.

`PORT` is read here and nowhere else: the service binds 0.0.0.0 so a mapped port
reaches it from outside the container, and it never needs a config file.
"""
from .server import serve

if __name__ == "__main__":
    serve()

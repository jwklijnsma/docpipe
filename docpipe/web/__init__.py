"""Self-hosted web UI for docpipe (`docpipe serve`)."""

from .server import WebApp, serve

__all__ = ["WebApp", "serve"]

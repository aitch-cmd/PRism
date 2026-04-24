from __future__ import annotations

from ._server import prs_server

# Importing the submodules for side effects — each one registers its tools
# on `prs_server` via @prs_server.tool decorators at import time.
from . import core, reviewers, authoring  # noqa: E402,F401

__all__ = ["prs_server"]

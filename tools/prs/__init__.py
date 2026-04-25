from __future__ import annotations

from ._server import prs_server

# Importing the submodules for side effects — each one registers its tools
# on `prs_server` via @prs_server.tool decorators at import time.
from . import (  # noqa: E402,F401
    core,
    reviewers,
    authoring,
    blast_radius,
    risk,
    incident,
)

__all__ = ["prs_server"]

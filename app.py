"""Thin application composition module for TeleScope."""

from __future__ import annotations

import os

from telescope_core.api import create_app


# A process-level override keeps browser and smoke-test instances independent
# from the user's long-lived archive. Normal launches still use ./data.
app = create_app(os.environ.get("TELESCOPE_DATA_DIR") or None)

__all__ = ["app", "create_app"]

"""Uvicorn entry point for the local TeleScope service."""

from __future__ import annotations

import os

from app import app

HOST = "127.0.0.1"
PORT = int(os.environ.get("TELESCOPE_PORT", "8000"))


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()

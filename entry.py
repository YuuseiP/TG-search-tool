"""Double-click friendly launcher that keeps the 8000-8010 port contract."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIRED = ("fastapi", "telethon", "pydantic", "python_socks", "httpx", "uvicorn")
ERROR_LOG = ROOT / "launch-error.log"


def available_port() -> int:
    for port in range(8000, 8011):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free TeleScope port was found in 8000-8010.")


def running_telescope_port() -> int | None:
    """Find an already-running TeleScope before opening the shared session again."""

    for port in range(8000, 8011):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.5) as response:
                if response.status == 200:
                    return port
        except (OSError, urllib.error.URLError):
            continue
    return None


def wait_until_ready(port: int, process: subprocess.Popen[object]) -> None:
    deadline = time.monotonic() + 12
    health_url = f"http://127.0.0.1:{port}/api/status"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"The service exited with code {process.returncode}.")
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise RuntimeError("The service did not pass its HTTP readiness check within 12 seconds.")


def main() -> int:
    missing = [name for name in REQUIRED if importlib.util.find_spec(name) is None]
    if missing:
        print("Installing the required dependencies for the first launch...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=ROOT)

    running_port = running_telescope_port()
    if running_port is not None:
        url = f"http://127.0.0.1:{running_port}"
        print(f"TeleScope is already running at {url}")
        webbrowser.open(url)
        return 0

    port = available_port()
    environment = {**os.environ, "TELESCOPE_PORT": str(port)}
    process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=environment)
    wait_until_ready(port, process)
    url = f"http://127.0.0.1:{port}"
    print(f"TeleScope is running at {url}")
    webbrowser.open(url)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        ERROR_LOG.write_text(f"Launch failed: {error}\n", encoding="utf-8")
        print(f"Launch failed. Details were written to {ERROR_LOG.name}.")
        raise SystemExit(1)

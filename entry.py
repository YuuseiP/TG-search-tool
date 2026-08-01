"""Double-click friendly local launcher for TeleScope."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED = ("fastapi", "telethon", "pydantic", "python_socks")
ERROR_LOG = ROOT / "launch-error.log"


def available_port() -> int:
    for port in range(8000, 8011):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("8000 到 8010 端口均被占用，请关闭一个本地服务后重试。")


def wait_until_ready(port: int, process: subprocess.Popen[object]) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"服务启动失败，退出码: {process.returncode}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError("服务未在 12 秒内响应。")


def main() -> int:
    missing = [name for name in REQUIRED if importlib.util.find_spec(name) is None]
    if missing:
        print("首次启动：正在安装所需依赖...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=ROOT)

    port = available_port()
    env = {**os.environ, "TELESCOPE_PORT": str(port)}
    process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env)
    wait_until_ready(port, process)
    url = f"http://127.0.0.1:{port}"
    print(f"TeleScope 正在运行：{url}")
    print("浏览器将自动打开。关闭此窗口或按 Ctrl+C 可停止服务。")
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
        detail = f"启动失败：{error}\n"
        ERROR_LOG.write_text(detail, encoding="utf-8")
        print(f"\n{detail}详细信息已写入 {ERROR_LOG.name}")
        raise SystemExit(1)

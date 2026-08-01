"""Small dependency-free local ASGI runner for TeleScope development."""

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable

from app import app, backfill_private_permalinks, initialize_db, remove_legacy_demo_data, shutdown_services

HOST = "127.0.0.1"
PORT = int(os.environ.get("TELESCOPE_PORT", "8000"))


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        header_data = await reader.readuntil(b"\r\n\r\n")
        lines = header_data.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        raw_path, _, query = target.partition("?")
        headers = [(key.strip().lower().encode(), value.strip().encode()) for line in lines[1:] if ":" in line for key, value in [line.split(":", 1)]]
        content_length = int(dict(headers).get(b"content-length", b"0"))
        body = await reader.readexactly(content_length) if content_length else b""
        received = False
        response_started = False

        async def receive() -> dict:
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                header_lines = b"".join(key + b": " + value + b"\r\n" for key, value in message.get("headers", []))
                writer.write(f"HTTP/1.1 {status} OK\r\n".encode() + header_lines + b"Connection: close\r\n\r\n")
                response_started = True
            elif message["type"] == "http.response.body":
                writer.write(message.get("body", b""))
                await writer.drain()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": method, "scheme": "http", "path": raw_path, "raw_path": raw_path.encode(), "query_string": query.encode(), "headers": headers, "client": (HOST, 0), "server": (HOST, PORT), "root_path": ""}
        await app(scope, receive, send)
        if not response_started:
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    except Exception as exc:
        payload = f'{{"detail":"{str(exc).replace(chr(34), chr(39))}"}}'.encode()
        writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def main() -> None:
    initialize_db()
    remove_legacy_demo_data()
    backfill_private_permalinks()
    server = await asyncio.start_server(handle, HOST, PORT)
    print(f"TeleScope is running at http://{HOST}:{PORT}")
    try:
        async with server:
            await server.serve_forever()
    finally:
        await shutdown_services()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())

"""External Telegram boundary with a strict saved-proxy policy."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from .observability import network_logger


class GatewayError(RuntimeError):
    """An external gateway failure with no credential-bearing detail."""


class TelegramGateway(Protocol):
    async def status(self, config: dict[str, Any]) -> dict[str, Any]: ...

    async def send_code(self, config: dict[str, Any]) -> dict[str, Any]: ...

    async def verify(self, config: dict[str, Any], code: str, password: str | None) -> dict[str, Any]: ...

    async def scan_dialogs(self, config: dict[str, Any]) -> list[dict[str, Any]]: ...

    async def iter_messages(self, dialog: dict[str, Any], limit: int | None, min_id: int = 0, max_id: int = 0): ...

    async def preview_image(self, config: dict[str, Any], channel_id: int, telegram_id: int) -> tuple[bytes, str]: ...

    async def video_thumbnail(self, config: dict[str, Any], channel_id: int, telegram_id: int) -> tuple[bytes, str]: ...

    async def close(self) -> None: ...


def public_gateway_error(error: BaseException) -> str:
    """Map arbitrary client errors to messages that cannot contain secrets."""

    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error).casefold():
        return "Telegram session is in use by another TeleScope window. Close the duplicate window and try again."
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, OSError)):
        return "Telegram connection timed out; check the saved Clash proxy."
    return "Telegram gateway request failed. Check the saved connection settings."


def gateway_error_code(error: BaseException) -> str:
    """Return a diagnostic category without putting connection settings in logs."""

    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error).casefold():
        return "session_database_locked"
    if isinstance(error, sqlite3.OperationalError):
        return "session_database_error"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "connection_timeout"
    if isinstance(error, OSError):
        return "connection_os_error"
    return type(error).__name__


def is_recoverable_history_error(error: BaseException) -> bool:
    """Identify connection resets that are safe to resume from the last ID."""

    if isinstance(error, (asyncio.TimeoutError, TimeoutError, OSError)):
        return True
    message = str(error).casefold()
    return any(marker in message for marker in (
        "wrong session id", "server closed the connection", "0 bytes read",
        "connection reset", "connection aborted", "incompleteread",
    ))


class RealTelegramGateway:
    """Telethon adapter; every connection is constructed with saved proxy data."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.client: Any = None
        self.phone_code_hash: str | None = None
        self._active_config: dict[str, Any] = {}
        self._client_key: tuple[Any, ...] | None = None
        self._authorized = False

    def local_status(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return startup-safe state without opening a Telegram connection."""

        return {
            "configured": bool(config.get("api_id") and config.get("api_hash")),
            "authorized": self._authorized,
            "session_saved": self.session_saved(),
        }

    def session_saved(self) -> bool:
        """Check for an authorized persistent Telethon session without network I/O."""

        path = self.data_dir / "telegram.session"
        if not path.is_file() or path.stat().st_size == 0:
            return False
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1) as database:
                row = database.execute("SELECT auth_key FROM sessions LIMIT 1").fetchone()
            return bool(row and row[0])
        except (OSError, sqlite3.Error):
            # A locked session is still a saved session. Keep its UI state and
            # let the normal restore request retry when the local lock clears.
            return True

    @staticmethod
    def proxy_from_config(config: dict[str, Any]) -> tuple[Any, ...]:
        proxy_type = str(config.get("proxy_type") or "socks5").lower()
        host = str(config.get("proxy_host") or "").strip()
        port = config.get("proxy_port")
        if proxy_type not in {"socks5", "http"} or not host or not port:
            raise GatewayError("A saved Clash proxy is required.")
        try:
            port = int(port)
        except (TypeError, ValueError) as error:
            raise GatewayError("The saved Clash proxy port is invalid.") from error
        if not 1 <= port <= 65535:
            raise GatewayError("The saved Clash proxy port is invalid.")
        username = str(config.get("proxy_username") or "").strip() or None
        password = config.get("proxy_password") or None
        return (proxy_type, host, port, username, password, True)

    async def _connect(self, config: dict[str, Any]) -> Any:
        self._active_config = dict(config)
        if not config.get("api_id") or not config.get("api_hash"):
            raise GatewayError("Telegram API settings are incomplete.")
        proxy = self.proxy_from_config(config)
        client_key = (int(config["api_id"]), str(config["api_hash"]), proxy)
        try:
            from telethon import TelegramClient

            if self.client is not None and self._client_key != client_key:
                with suppress(Exception):
                    await self.client.disconnect()
                self.client = None
                self._client_key = None
            if self.client is None:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self.client = TelegramClient(
                    str(self.data_dir / "telegram"), int(config["api_id"]), str(config["api_hash"]),
                    proxy=proxy, connection_retries=2, retry_delay=1, timeout=12,
                )
                self._client_key = client_key
            connected = self.client.is_connected()
            if not connected:
                network_logger().info("telegram_connect_start proxy_type=%s proxy_port=%s", proxy[0], proxy[2])
                await asyncio.wait_for(self.client.connect(), timeout=15)
                network_logger().info("telegram_connect_ready")
            return self.client
        except GatewayError:
            raise
        except Exception as error:
            with suppress(Exception):
                if self.client is not None:
                    await self.client.disconnect()
            self.client = None
            self._client_key = None
            network_logger().warning("telegram_connect_failed reason=%s", gateway_error_code(error))
            raise GatewayError(public_gateway_error(error)) from error

    async def _discard_client(self) -> None:
        """Discard protocol state after a transport reset before reconnecting."""

        if self.client is not None:
            with suppress(Exception):
                await self.client.disconnect()
        self.client = None
        self._client_key = None

    async def status(self, config: dict[str, Any]) -> dict[str, Any]:
        if not config.get("api_id") or not config.get("api_hash"):
            return {"configured": False, "authorized": False, "session_saved": self.session_saved()}
        try:
            client = await self._connect(config)
            self._authorized = bool(await client.is_user_authorized())
            return {"configured": True, "authorized": self._authorized, "session_saved": self.session_saved()}
        except Exception:
            return {"configured": True, "authorized": False, "session_saved": self.session_saved()}

    async def send_code(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            network_logger().info("telegram_send_code_start")
            client = await self._connect(config)
            self._authorized = bool(await client.is_user_authorized())
            if self._authorized:
                return {"authorized": True, "sent": False}
            sent = await asyncio.wait_for(client.send_code_request(config["phone"]), timeout=15)
            self.phone_code_hash = sent.phone_code_hash
            network_logger().info("telegram_send_code_ready")
            return {"authorized": False, "sent": True}
        except GatewayError:
            network_logger().warning("telegram_send_code_failed type=gateway")
            raise
        except Exception as error:
            network_logger().warning("telegram_send_code_failed type=%s", type(error).__name__)
            raise GatewayError(public_gateway_error(error)) from error

    async def verify(self, config: dict[str, Any], code: str, password: str | None) -> dict[str, Any]:
        try:
            network_logger().info("telegram_verify_start has_password=%s", bool(password))
            client = await self._connect(config)
            try:
                await asyncio.wait_for(
                    client.sign_in(phone=config["phone"], code=code, phone_code_hash=self.phone_code_hash), timeout=15
                )
            except Exception as error:
                from telethon.errors import SessionPasswordNeededError

                if isinstance(error, SessionPasswordNeededError) and password:
                    await asyncio.wait_for(client.sign_in(password=password), timeout=15)
                else:
                    raise GatewayError("Telegram verification failed.") from error
            self._authorized = True
            return {"authorized": True}
        except GatewayError:
            network_logger().warning("telegram_verify_failed type=gateway")
            raise
        except Exception as error:
            network_logger().warning("telegram_verify_failed type=%s", type(error).__name__)
            raise GatewayError(public_gateway_error(error)) from error

    async def scan_dialogs(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            client = await self._connect(config)
            if not await client.is_user_authorized():
                raise GatewayError("Telegram login is required.")
            items: list[dict[str, Any]] = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if not (getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)):
                    continue
                items.append({
                    "id": int(entity.id), "title": str(dialog.name or entity.id),
                    "username": getattr(entity, "username", None),
                    "kind": "channel" if getattr(entity, "broadcast", False) else "group",
                })
            return sorted(items, key=lambda item: item["title"].casefold())
        except GatewayError:
            raise
        except Exception as error:
            raise GatewayError(public_gateway_error(error)) from error

    async def iter_messages(self, dialog: dict[str, Any], limit: int | None, min_id: int = 0, max_id: int = 0):
        """Read history with bounded reconnects and lossless archive deduplication."""

        remaining = limit
        window_max_id = max_id
        resume_max_id = window_max_id
        retries = 0
        while remaining is None or remaining > 0:
            try:
                client = await self._connect(getattr(self, "_active_config", {}))
                entity = await client.get_entity(dialog["id"])
                stream = client.iter_messages(entity, limit=remaining, min_id=min_id or 0, max_id=resume_max_id or 0)
                async for message in stream:
                    message_id = int(getattr(message, "id", 0) or 0)
                    if message_id:
                        resume_max_id = message_id
                    if remaining is not None:
                        remaining -= 1
                    yield message
                return
            except GatewayError:
                raise
            except Exception as error:
                if retries >= 2 or not is_recoverable_history_error(error):
                    network_logger().warning("telegram_history_failed channel_id=%s reason=%s", dialog["id"], gateway_error_code(error))
                    raise GatewayError(public_gateway_error(error)) from error
                retries += 1
                network_logger().warning(
                    "telegram_history_retry channel_id=%s retry=%s restart_window=%s reason=%s",
                    dialog["id"], retries, bool(resume_max_id), gateway_error_code(error),
                )
                # The caller commits message batches. A message yielded just
                # before a broken connection may still be waiting in memory,
                # so restart this window and let the unique archive key dedupe
                # it instead of advancing past an uncommitted message.
                resume_max_id = window_max_id
                remaining = limit
                await self._discard_client()
                await asyncio.sleep(retries)

    async def preview_image(self, config: dict[str, Any], channel_id: int, telegram_id: int) -> tuple[bytes, str]:
        """Fetch one visible Telegram photo into memory only; never write media to disk."""

        try:
            network_logger().info("telegram_preview_start channel_id=%s", channel_id)
            client = await self._connect(config)
            if not await client.is_user_authorized():
                raise GatewayError("Telegram login is required.")
            entity = await client.get_entity(channel_id)
            message = await asyncio.wait_for(client.get_messages(entity, ids=telegram_id), timeout=15)
            # Archived "image" records can be Telegram photos or image
            # documents. RichMessage posts keep their photos in a separate
            # list and leave the legacy `media` field empty.
            rich = getattr(message, "rich_message", None) if message is not None else None
            photos = getattr(rich, "photos", None) if rich is not None else None
            target = photos[0] if isinstance(photos, (list, tuple)) and photos else message
            if target is None or (target is message and not getattr(message, "media", None)):
                raise GatewayError("The requested Telegram image is unavailable.")
            content = await asyncio.wait_for(client.download_media(target, file=bytes), timeout=30)
            if not isinstance(content, bytes) or not content:
                raise GatewayError("The requested Telegram image is unavailable.")
            network_logger().info("telegram_preview_ready channel_id=%s bytes=%s", channel_id, len(content))
            return content, "image/jpeg"
        except GatewayError as error:
            network_logger().warning("telegram_preview_failed type=gateway reason=%s", str(error))
            raise
        except Exception as error:
            network_logger().warning("telegram_preview_failed type=%s", type(error).__name__)
            raise GatewayError(public_gateway_error(error)) from error

    async def video_thumbnail(self, config: dict[str, Any], channel_id: int, telegram_id: int) -> tuple[bytes, str]:
        """Fetch Telegram's largest available thumbnail, never the video payload."""

        try:
            network_logger().info("telegram_thumbnail_start channel_id=%s", channel_id)
            client = await self._connect(config)
            if not await client.is_user_authorized():
                raise GatewayError("Telegram login is required.")
            entity = await client.get_entity(channel_id)
            message = await asyncio.wait_for(client.get_messages(entity, ids=telegram_id), timeout=15)
            if message is None or not getattr(message, "media", None):
                raise GatewayError("The requested Telegram video thumbnail is unavailable.")
            # ``thumb=-1`` selects the largest thumbnail rendition. ``thumb=0``
            # selected the tiny placeholder layer responsible for blurry cards.
            content = await asyncio.wait_for(client.download_media(message, file=bytes, thumb=-1), timeout=20)
            if not isinstance(content, bytes) or not content:
                raise GatewayError("The requested Telegram video thumbnail is unavailable.")
            network_logger().info("telegram_thumbnail_ready channel_id=%s bytes=%s", channel_id, len(content))
            return content, "image/jpeg"
        except GatewayError as error:
            network_logger().warning("telegram_thumbnail_failed type=gateway reason=%s", str(error))
            raise
        except Exception as error:
            network_logger().warning("telegram_thumbnail_failed type=%s", type(error).__name__)
            raise GatewayError(public_gateway_error(error)) from error

    async def sync(
        self,
        dialogs: list[dict[str, Any]],
        limit: int | None,
        on_message: Callable[[dict[str, Any], Any], Awaitable[None]],
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        # This method is useful to fakes and keeps all Telethon iteration inside
        # the gateway boundary. The service normally uses iter_messages so each
        # channel can be persisted independently and resumed.
        config: dict[str, Any] = getattr(self, "_active_config", {})
        client = await self._connect(config)
        for dialog in dialogs:
            if should_cancel and should_cancel():
                raise asyncio.CancelledError
            entity = await client.get_entity(dialog["id"])
            async for message in client.iter_messages(entity, limit=limit):
                if should_cancel and should_cancel():
                    raise asyncio.CancelledError
                await on_message(dialog, message)

    async def close(self) -> None:
        if self.client is not None:
            with suppress(Exception):
                await self.client.disconnect()
            self.client = None
            self._client_key = None

    async def reset(self) -> None:
        """Drop the cached Telethon client after connection settings change."""

        await self.close()

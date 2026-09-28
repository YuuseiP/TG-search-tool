from __future__ import annotations

import asyncio
import inspect
import json
import re
import sqlite3
import subprocess
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import httpx

import app
import entry
from telescope_core.providers import HttpxAIGateway, ProviderError


ROOT = Path(__file__).resolve().parents[1]
STATIC_APP = ROOT / "static" / "app.js"
ENTRYPOINT = ROOT / "entry.py"
WINDOWS_LAUNCHER = next(ROOT.glob("*.bat"))
TEST_TMP_ROOT = Path("D:/tmp")
TEST_MESSAGES = {
    101: [
        {
            "telegram_id": 1,
            "sent_at": "2026-08-01T00:00:00+00:00",
            "text": "#alpha #beta shared BODY_SHOULD_NEVER_REACH_AI",
            "media_type": "text",
            "sender": "sender-a",
        },
        {
            "telegram_id": 2,
            "sent_at": "2026-08-01T01:00:00+00:00",
            "text": "#alpha only text",
            "media_type": "text",
            "sender": "sender-b",
        },
        {
            "telegram_id": 3,
            "sent_at": "2026-08-01T02:00:00+00:00",
            "text": "album caption token #album",
            "media_type": "image",
            "grouped_id": "album-1",
            "album_text": "album caption token",
            "sender": "sender-c",
        },
        {
            "telegram_id": 4,
            "sent_at": "2026-08-01T03:00:00+00:00",
            "text": "#album",
            "media_type": "image",
            "grouped_id": "album-1",
            "album_text": "album caption token",
            "sender": "sender-c",
        },
    ],
    102: [
        {
            "telegram_id": 1,
            "sent_at": "2026-08-01T04:00:00+00:00",
            "text": "#gamma group-only text",
            "media_type": "text",
            "sender": "sender-d",
        }
    ],
}


def _value(payload: Any, *names: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        for name in names:
            if name in payload:
                return payload[name]
    return default


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    value = _value(payload, "items", "results", "tags", "channels", "dialogs", "jobs", "sync_jobs", default=[])
    if isinstance(value, list):
        return value
    return []


def _job(payload: Any) -> dict[str, Any]:
    value = _value(payload, "job", default=payload)
    if not isinstance(value, dict):
        raise AssertionError(f"expected job object, got {payload!r}")
    return value


def _job_id(payload: Any) -> str:
    value = _value(_job(payload), "job_id", "id")
    if not value:
        raise AssertionError(f"response has no job id: {payload!r}")
    return str(value)


def _tag_names(payload: Any) -> list[str]:
    values = _value(payload, "tags", "items", "results", default=[])
    if not isinstance(values, list):
        return []
    names = []
    for value in values:
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, dict):
            name = _value(value, "tag", "name")
            if isinstance(name, str):
                names.append(name)
    return names


def _message_tags(item: dict[str, Any]) -> set[str]:
    value = item.get("tags", [])
    raw = " ".join(value) if isinstance(value, list) else str(value)
    return set(re.findall(r"#[A-Za-z0-9_]+", raw))


def _interpretation(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("interpretation")
    if not isinstance(value, dict):
        raise AssertionError(f"AI search did not return an interpretation: {payload!r}")
    return value


def _trace(interpretation: dict[str, Any]) -> list[dict[str, Any]]:
    relaxation = interpretation.get("relaxation") if isinstance(interpretation, dict) else None
    if isinstance(relaxation, dict):
        value = relaxation.get("steps", [])
    else:
        value = _value(interpretation, "relaxation_trace", "trace", default=[])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AssertionError(f"AI search did not return a relaxation trace: {interpretation!r}")
    return value


def _trace_count(step: dict[str, Any]) -> int:
    value = _value(step, "count", "result_count", "total")
    if not isinstance(value, int):
        raise AssertionError(f"trace step has no integer count: {step!r}")
    return value


def _trace_tags(step: dict[str, Any]) -> list[str]:
    value = _value(step, "tags", "selected_tags", "remaining_tags", default=[])
    return list(value) if isinstance(value, list) else []


def _trace_removed(step: dict[str, Any]) -> str | None:
    value = _value(step, "removed_tag", "removed", "dropped_tag")
    return value if isinstance(value, str) else None


def _create_application(data_dir: Path, telegram_gateway: Any, ai_gateway: Any) -> Any:
    """Build the app using its injectable factory without touching repository data."""
    factory = getattr(app, "create_app", None)
    if factory is None:
        raise AssertionError("the frozen backend contract requires app.create_app")

    values = {
        "data_dir": data_dir,
        "storage_dir": data_dir,
        "root_dir": data_dir,
        "telegram_gateway": telegram_gateway,
        "telegram": telegram_gateway,
        "telegram_service": telegram_gateway,
        "ai_gateway": ai_gateway,
        "ai": ai_gateway,
        "ai_service": ai_gateway,
        "model_gateway": ai_gateway,
        "db_path": data_dir / "telescope.db",
        "database_path": data_dir / "telescope.db",
        "config_path": data_dir / "config.json",
    }
    signature = inspect.signature(factory)
    kwargs = {
        name: values[name]
        for name, parameter in signature.parameters.items()
        if name in values
        and parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    missing = [
        name
        for name, parameter in signature.parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in kwargs
    ]
    if missing:
        raise AssertionError(f"create_app has unsupported required parameters: {missing}")
    return factory(**kwargs)


class FakeTelegramGateway:
    def __init__(self) -> None:
        self.dialogs = [
            {"id": 101, "title": "Alpha feed", "username": "alpha_feed", "kind": "channel"},
            {"id": 102, "title": "Group room", "username": None, "kind": "group"},
        ]
        self.messages = {channel_id: [dict(item) for item in items] for channel_id, items in TEST_MESSAGES.items()}
        self.fetch_calls: list[tuple[int, int | None]] = []
        self.fetch_windows: list[tuple[int, int | None, int, int]] = []
        self.scan_calls = 0
        self.authorized = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block_sync = False
        self.auth_calls: list[tuple[str, Any]] = []
        self.preview_calls: list[tuple[int, int]] = []

    async def status(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"configured": True, "authorized": self.authorized, "phone": "+00000000000"}

    async def scan_dialogs(self, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.scan_calls += 1
        return [dict(item) for item in self.dialogs]

    async def scan(self, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self.scan_dialogs(config)

    async def get_dialogs(self, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self.scan_dialogs(config)

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def connect(self) -> "FakeTelegramGateway":
        return self

    async def iter_messages(self, dialog: Any, limit: int | None = None, min_id: int = 0, max_id: int = 0):
        channel_id = dialog["id"] if isinstance(dialog, dict) else int(dialog)
        self.fetch_calls.append((channel_id, limit))
        self.fetch_windows.append((channel_id, limit, min_id, max_id))
        self.started.set()
        if self.block_sync:
            await self.release.wait()
        selected = self.messages.get(channel_id, [])
        fresh = [message for message in selected if int(message.get("telegram_id", message.get("id", 0))) > min_id]
        if max_id:
            fresh = [message for message in fresh if int(message.get("telegram_id", message.get("id", 0))) < max_id]
        for message in fresh if limit is None else fresh[:limit]:
            yield dict(message)

    async def fetch_messages(self, dialog: Any, limit: int | None = None) -> list[dict[str, Any]]:
        return [item async for item in self.iter_messages(dialog, limit)]

    async def sync(self, dialogs: list[dict[str, Any]], limit: int | None, job: Any = None) -> dict[int, list[dict[str, Any]]]:
        fetched: dict[int, list[dict[str, Any]]] = {}
        for dialog in dialogs:
            channel_id = int(dialog["id"])
            messages = await self.fetch_messages(dialog, limit)
            fetched[channel_id] = messages
            if job is not None and hasattr(job, "results"):
                job.results.append(
                    {
                        "channel_id": channel_id,
                        "title": dialog.get("title", str(channel_id)),
                        "state": "completed",
                        "processed": len(messages),
                        "stored_total": len(messages),
                        "error": None,
                    }
                )
                if hasattr(job, "channels_completed"):
                    job.channels_completed += 1
                if hasattr(job, "messages_processed"):
                    job.messages_processed += len(messages)
        return fetched

    async def send_code(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        self.auth_calls.append(("send_code", config))
        return {"sent": True, "authorized": self.authorized}

    async def verify(self, config: dict[str, Any], code: str, password: str | None = None) -> dict[str, Any]:
        self.auth_calls.append(("verify", {"config": config, "code": code, "password": password}))
        self.authorized = True
        return {"authorized": True}

    async def preview_image(self, config: dict[str, Any], channel_id: int, telegram_id: int) -> tuple[bytes, str]:
        self.preview_calls.append((channel_id, telegram_id))
        return (b"\x89PNG\r\n\x1a\nfixture-preview", "image/png")

    async def video_thumbnail(self, config: dict[str, Any], channel_id: int, telegram_id: int) -> tuple[bytes, str]:
        self.preview_calls.append((channel_id, telegram_id))
        return (b"\x89PNG\r\n\x1a\nfixture-thumbnail", "image/png")

    async def sign_in(self, code: str, password: str | None = None) -> dict[str, Any]:
        return await self.verify({}, code, password)

    async def close(self) -> None:
        return None


class LocalStatusTelegramGateway(FakeTelegramGateway):
    def __init__(self) -> None:
        super().__init__()
        self.remote_status_calls = 0

    def local_status(self, config: dict[str, Any]) -> dict[str, Any]:
        return {"configured": bool(config.get("api_id") and config.get("api_hash")), "authorized": False}

    async def status(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        self.remote_status_calls += 1
        await asyncio.Event().wait()
        return {"configured": True, "authorized": True}


class RecordingAIGateway:
    def __init__(self) -> None:
        self.model_calls: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.local_failure = False
        self.timeout = False
        self.block_models = False
        self.models_started = asyncio.Event()
        self.models_release = asyncio.Event()
        self.tag_response: dict[str, Any] = {
            "tags": ["#alpha", "#beta", "#gamma", "#made-up"],
        }

    async def list_models(self, provider: str, base_url: str, api_key: str = "") -> list[str]:
        call = {"provider": provider, "base_url": base_url, "api_key": api_key}
        self.model_calls.append(call)
        if self.timeout:
            raise TimeoutError("fake gateway timeout")
        if provider == "local" and self.local_failure:
            raise OSError("fake local gateway offline")
        if self.block_models:
            self.models_started.set()
            await self.models_release.wait()
        return [f"{provider}-fixture-model"]

    async def models(self, provider: str, base_url: str, api_key: str = "") -> list[str]:
        return await self.list_models(provider, base_url, api_key)

    async def discover_models(self, provider: str, base_url: str, api_key: str = "") -> list[str]:
        return await self.list_models(provider, base_url, api_key)

    async def select_tags(self, **payload: Any) -> dict[str, Any]:
        self.requests.append(dict(payload))
        return dict(self.tag_response)

    async def search_tags(self, **payload: Any) -> dict[str, Any]:
        return await self.select_tags(**payload)

    async def interpret(
        self,
        provider: str,
        base_url: str,
        model: str,
        api_key: str,
        query: str,
        vocabulary: list[str],
        tags: list[str],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.select_tags(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            query=query,
            vocabulary=vocabulary,
            tags=tags,
            config=config,
        )

    async def test(self, provider: str, base_url: str, model: str, api_key: str = "") -> dict[str, str]:
        if self.timeout:
            raise TimeoutError("fake gateway timeout")
        if provider == "local" and self.local_failure:
            raise OSError("fake local gateway offline")
        return {"provider": provider, "model": model}

    async def test_connection(self, provider: str, base_url: str, model: str, api_key: str = "") -> dict[str, str]:
        return await self.test(provider, base_url, model, api_key)

    async def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = dict(kwargs)
        if args:
            payload["args"] = args
        self.requests.append(payload)
        return dict(self.tag_response)

    async def close(self) -> None:
        return None


class HttpxAIGatewayTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _gateway(handler: Any, **kwargs: Any) -> HttpxAIGateway:
        gateway = HttpxAIGateway(**kwargs)
        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return gateway

    async def test_concurrent_identical_reads_share_one_mock_transport_call(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        url = "https://fixture.invalid/models"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]}, request=request)

        gateway = self._gateway(handler)
        try:
            leader = asyncio.create_task(gateway.request_json("GET", url, cache=False))
            await asyncio.wait_for(started.wait(), timeout=1)
            follower = asyncio.create_task(gateway.request_json("GET", url, cache=False))
            await asyncio.sleep(0)
            self.assertEqual(calls, 1)
            release.set()
            self.assertEqual(await asyncio.gather(leader, follower), [{"data": [{"id": "fixture-model"}]}] * 2)
        finally:
            await gateway.close()

    async def test_cache_expiry_forces_a_new_mock_transport_call(self) -> None:
        calls = 0
        url = "https://fixture.invalid/models"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"calls": calls}, request=request)

        gateway = self._gateway(handler)
        try:
            first = await gateway.request_json("GET", url)
            second = await gateway.request_json("GET", url)
            self.assertEqual(first, {"calls": 1})
            self.assertEqual(second, first)
            self.assertEqual(calls, 1)

            key = gateway._request_key("GET", url, None, "")
            entry = gateway._cache[key]
            gateway._cache[key] = type(entry)(expires_at=0, value=entry.value)
            expired = await gateway.request_json("GET", url)
            self.assertEqual(expired, {"calls": 2})
            self.assertEqual(calls, 2)
        finally:
            await gateway.close()

    async def test_get_retry_is_bounded_to_two_attempts(self) -> None:
        calls = 0
        url = "https://fixture.invalid/models"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("temporary fixture failure", request=request)

        gateway = self._gateway(handler, overall_timeout=1)
        try:
            with self.assertRaises(ProviderError) as context:
                await gateway.request_json("GET", url, cache=False)
            self.assertEqual(context.exception.status_code, 504)
            self.assertEqual(calls, 2)
        finally:
            await gateway.close()

    async def test_post_transport_failure_is_sent_once(self) -> None:
        calls = 0
        url = "https://fixture.invalid/chat/completions"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("fixture read timeout", request=request)

        gateway = self._gateway(handler, overall_timeout=1)
        try:
            with self.assertRaises(ProviderError) as context:
                await gateway.request_json("POST", url, {"query": "fixture"}, cache=False)
            self.assertEqual(context.exception.status_code, 504)
            self.assertEqual(calls, 1)
        finally:
            await gateway.close()

    async def test_cancelling_one_reader_keeps_shared_request_available(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        url = "https://fixture.invalid/models"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True}, request=request)

        gateway = self._gateway(handler)
        try:
            cancelled_reader = asyncio.create_task(gateway.request_json("GET", url, cache=False))
            await asyncio.wait_for(started.wait(), timeout=1)
            cancelled_reader.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_reader

            remaining_reader = asyncio.create_task(gateway.request_json("GET", url, cache=False))
            await asyncio.sleep(0)
            self.assertEqual(calls, 1)
            release.set()
            self.assertEqual(await remaining_reader, {"ok": True})
        finally:
            await gateway.close()

    async def test_overall_deadline_stops_a_hanging_mock_request(self) -> None:
        started = asyncio.Event()
        never = asyncio.Event()
        calls = 0
        url = "https://fixture.invalid/models"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            started.set()
            await never.wait()
            return httpx.Response(200, json={"ok": True}, request=request)

        gateway = self._gateway(handler, overall_timeout=0.02)
        try:
            request = asyncio.create_task(gateway.request_json("GET", url, cache=False))
            await asyncio.wait_for(started.wait(), timeout=1)
            with self.assertRaises(ProviderError) as context:
                await request
            self.assertEqual(context.exception.status_code, 504)
            self.assertEqual(calls, 1)
        finally:
            await gateway.close()


class ApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="telescope-test-", dir=TEST_TMP_ROOT)
        self.storage = Path(self.temp_dir.name)
        self.telegram = FakeTelegramGateway()
        self.ai = RecordingAIGateway()
        self.application = _create_application(self.storage, self.telegram, self.ai)
        self.transport = httpx.ASGITransport(app=self.application)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://fixture")

    async def asyncTearDown(self) -> None:
        try:
            await self.application.state.service.stop()
        finally:
            try:
                await self.client.aclose()
            finally:
                self.temp_dir.cleanup()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self.client.request(method, path, **kwargs)

    async def wait_for_completed_sync(self, client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
        for _ in range(100):
            status_response = await client.get(f"/api/sync/{job_id}")
            self.assertEqual(status_response.status_code, 200, status_response.text)
            status = _job(status_response.json())
            if status.get("state") in {"completed", "failed", "cancelled"}:
                self.assertEqual(status.get("state"), "completed", status_response.text)
                return status
            await asyncio.sleep(0.005)
        self.fail(f"sync job {job_id} did not finish")

    async def scan(self) -> list[dict[str, Any]]:
        response = await self.request("POST", "/api/channels/scan")
        self.assertEqual(response.status_code, 200, response.text)
        return _items(response.json())

    async def sync(self, channel_ids: list[int], limit: int | None = 50) -> dict[str, Any]:
        response = await self.request(
            "POST",
            "/api/sync",
            json={"channel_ids": channel_ids, "limit_per_channel": limit},
        )
        self.assertIn(response.status_code, (200, 202), response.text)
        job_id = _job_id(response.json())
        for _ in range(100):
            status_response = await self.request("GET", f"/api/sync/{job_id}")
            self.assertEqual(status_response.status_code, 200, status_response.text)
            status = _job(status_response.json())
            if status.get("state") in {"completed", "failed", "cancelled"}:
                self.assertEqual(status.get("state"), "completed", status_response.text)
                return status
            await asyncio.sleep(0.005)
        self.fail(f"sync job {job_id} did not finish")

    async def seed_archive(self) -> None:
        await self.scan()
        await self.sync([101, 102], None)

    async def test_fixture_storage_and_transport_are_isolated(self) -> None:
        storage = self.storage.resolve()
        self.assertTrue(storage.is_relative_to(TEST_TMP_ROOT.resolve()))
        self.assertIsInstance(self.transport, httpx.ASGITransport)
        self.assertEqual(self.client.base_url.host, "fixture")
        self.assertEqual(self.telegram.scan_calls, 0)
        self.assertEqual(self.ai.requests, [])

    async def test_empty_telegram_placeholder_does_not_erase_archived_message(self) -> None:
        await self.scan()
        dialog = self.telegram.dialogs[0]
        store = self.application.state.service.store
        store.upsert_messages(dialog, [{
            "telegram_id": 700,
            "sent_at": "2026-08-01T00:00:00+00:00",
            "text": "标题\n正文 https://example.com/file",
            "media_type": "image",
            "grouped_id": "album-700",
        }])
        store.upsert_messages(dialog, [{
            "telegram_id": 700,
            "sent_at": "2026-08-01T00:00:00+00:00",
            "text": "",
            "media_type": "text",
        }])
        response = await self.request("POST", "/api/search", json={"query": "标题", "include_hidden_ads": True, "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        item = next(item for item in response.json()["items"] if item["telegram_id"] == 700)
        self.assertEqual(item["text"], "标题\n正文 https://example.com/file")
        self.assertEqual(item["media_type"], "image")
        self.assertEqual(item["grouped_id"], "album-700")

    async def test_rich_message_blocks_are_indexed_as_image_caption_text(self) -> None:
        await self.scan()
        dialog = self.telegram.dialogs[0]
        self.application.state.service.store.upsert_messages(dialog, [{
            "telegram_id": 701,
            "sent_at": "2026-08-01T00:00:00+00:00",
            "text": "",
            "media": None,
            "rich_message": {
                "photos": [{"id": 1}],
                "blocks": [
                    {"text": {"text": "c8|"}},
                    {"photo_id": 1},
                    {"text": {"text": "动画标题"}},
                    {"text": {"texts": [{"text": "正文链接"}, {"text": " #动画"}]}},
                ],
            },
        }])
        response = await self.request("POST", "/api/search", json={"query": "动画标题", "include_hidden_ads": True, "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        item = next(item for item in response.json()["items"] if item["telegram_id"] == 701)
        self.assertEqual(item["text"], "c8|\n动画标题\n正文链接 #动画")
        self.assertEqual(item["media_type"], "image")

    async def test_rich_message_document_is_not_imported_as_empty_text(self) -> None:
        await self.scan()
        dialog = self.telegram.dialogs[0]
        self.application.state.service.store.upsert_messages(dialog, [{
            "telegram_id": 702,
            "sent_at": "2026-08-01T00:00:00+00:00",
            "text": "",
            "media": None,
            "rich_message": {
                "photos": [],
                "documents": [{"mime_type": "audio/ogg"}],
                "blocks": [{"text": {"text": "音频资源 #音声"}}],
            },
        }])
        response = await self.request("POST", "/api/search", json={"query": "音频资源", "include_hidden_ads": True, "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        item = next(item for item in response.json()["items"] if item["telegram_id"] == 702)
        self.assertEqual(item["text"], "音频资源 #音声")
        self.assertEqual(item["media_type"], "audio")
        self.assertEqual(item["tags"], "#音声")

    async def test_status_prefers_local_gateway_state_without_a_network_call(self) -> None:
        gateway = LocalStatusTelegramGateway()
        application = _create_application(self.storage, gateway, RecordingAIGateway())
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
                response = await asyncio.wait_for(client.get("/api/status"), timeout=0.2)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"configured": False, "authorized": False, "session_saved": False, "phone": ""})
        self.assertEqual(gateway.remote_status_calls, 0)

    async def test_status_restore_uses_the_persisted_gateway_session_automatically(self) -> None:
        response = await self.request("GET", "/api/status?restore=true")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["authorized"])

    async def test_frozen_routes_keep_archive_operations_and_remove_ai_index(self) -> None:
        paths = {route.path for route in self.application.routes}
        for path in (
            "/api/status",
            "/api/settings",
            "/api/auth/send-code",
            "/api/auth/verify",
            "/api/client-events",
            "/api/channels/scan",
            "/api/channels",
            "/api/dialogs",
            "/api/sync",
            "/api/search",
            "/api/tags",
            "/api/messages/{message_id}/bookmark",
            "/api/messages/{message_id}/preview",
            "/api/messages/{message_id}/thumbnail",
            "/api/ai/settings",
            "/api/ai/search",
            "/api/ai/models",
            "/api/ai/test",
        ):
            self.assertIn(path, paths)
        self.assertFalse(any(path == "/api/ai/index" or path.startswith("/api/ai/index/") for path in paths))
        response = await self.request("GET", "/api/ai/index")
        self.assertEqual(response.status_code, 404, response.text)
        post_response = await self.request("POST", "/api/ai/index", json={})
        self.assertEqual(post_response.status_code, 404, post_response.text)
        sync_index = await self.request("GET", "/api/sync")
        self.assertEqual(sync_index.status_code, 200, sync_index.text)

    async def test_auth_and_proxy_settings_use_fixture_gateway_and_persist(self) -> None:
        payload = {
            "api_id": 123,
            "api_hash": "FAKE_HASH",
            "phone": "+00000000000",
            "proxy_type": "socks5",
            "proxy_host": "proxy.fixture",
            "proxy_port": 7890,
            "proxy_username": "fixture-user",
            "proxy_password": "fixture-password",
        }
        saved = await self.request("POST", "/api/settings", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        send_code = await self.request("POST", "/api/auth/send-code")
        self.assertEqual(send_code.status_code, 200, send_code.text)
        verified = await self.request("POST", "/api/auth/verify", json={"code": "000000", "password": "fixture-password"})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual([name for name, _ in self.telegram.auth_calls], ["send_code", "verify"])

        restored = await self.request("GET", "/api/settings")
        self.assertEqual(restored.status_code, 200, restored.text)
        restored_body = restored.json()
        self.assertEqual(restored_body["api_id"], 123)
        self.assertEqual(restored_body["phone"], "+00000000000")
        self.assertEqual(restored_body["proxy_host"], "proxy.fixture")
        self.assertTrue(restored_body.get("api_hash_saved", True))
        self.assertTrue(restored_body.get("proxy_password_saved", True))

    async def test_scan_classifies_channel_and_group_and_selected_sync_persists(self) -> None:
        items = await self.scan()
        self.assertEqual({item["id"] for item in items}, {101, 102})
        self.assertEqual({item["kind"] for item in items}, {"channel", "group"})
        dialogs = await self.request("GET", "/api/dialogs")
        self.assertEqual(dialogs.status_code, 200, dialogs.text)
        self.assertEqual({item["id"] for item in _items(dialogs.json())}, {101, 102})

        progress = await self.sync([101], 2)
        self.assertEqual(self.telegram.fetch_calls, [(101, 2)])
        self.assertEqual(progress["channels_completed"], 1)
        self.assertEqual(progress["messages_processed"], 2)
        self.assertEqual(len(progress["results"]), 1)
        channels = await self.request("GET", "/api/channels")
        self.assertEqual(channels.status_code, 200, channels.text)
        channel = next(item for item in _items(channels.json()) if item["id"] == 101)
        self.assertEqual(channel["message_count"], 2)
        self.assertNotIn(102, {item["id"] for item in _items(channels.json())})

    async def test_search_groups_persist_fixed_channels_and_prune_deleted_sources(self) -> None:
        await self.seed_archive()
        created = await self.request("POST", "/api/search-groups", json={"name": "Primary sources", "channel_ids": [101, 102]})
        self.assertEqual(created.status_code, 201, created.text)
        group = created.json()
        self.assertEqual(group["channel_ids"], [101, 102])
        listed = await self.request("GET", "/api/search-groups")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), [group])
        updated = await self.request(
            "PUT", f"/api/search-groups/{group['id']}", json={"name": "Focused sources", "channel_ids": [101]}
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        group = updated.json()
        self.assertEqual(group["name"], "Focused sources")
        self.assertEqual(group["channel_ids"], [101])
        search = await self.request("POST", "/api/search", json={"channel_ids": group["channel_ids"], "limit": 100})
        self.assertEqual(search.status_code, 200, search.text)
        self.assertEqual({item["channel_id"] for item in search.json()["items"]}, {101})
        deleted = await self.request("DELETE", "/api/channels/102")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        pruned = await self.request("GET", "/api/search-groups")
        self.assertEqual(pruned.status_code, 200, pruned.text)
        self.assertEqual(pruned.json()[0]["channel_ids"], [101])
        removed = await self.request("DELETE", f"/api/search-groups/{group['id']}")
        self.assertEqual(removed.status_code, 200, removed.text)
        empty = await self.request("GET", "/api/search-groups")
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), [])

    async def test_all_history_passes_unbounded_limit_and_links_are_correct(self) -> None:
        await self.scan()
        await self.sync([101], None)
        self.assertEqual(self.telegram.fetch_calls[-1], (101, None))
        response = await self.request("POST", "/api/search", json={"query": "shared", "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["permalink"], "https://t.me/alpha_feed/1")

        private_sync = await self.sync([102], 50)
        self.assertEqual(private_sync["state"], "completed")
        response = await self.request("POST", "/api/search", json={"query": "group-only", "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["permalink"], "https://t.me/c/102/1")

    async def test_album_deduplication_fts_filters_and_bookmarks(self) -> None:
        await self.seed_archive()
        album = await self.request("POST", "/api/search", json={"query": "album caption token", "limit": 20})
        self.assertEqual(album.status_code, 200, album.text)
        self.assertEqual(album.json()["total"], 1)
        self.assertEqual(album.json()["items"][0]["telegram_id"], 3)
        empty = await self.request("POST", "/api/search", json={"query": "", "limit": 20})
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["total"], 4)

        filtered = await self.request(
            "POST",
            "/api/search",
            json={
                "query": "text",
                "channel_ids": [101],
                "media_types": ["text"],
                "from_date": "2026-08-01",
                "until_date": "2026-08-01",
                "limit": 20,
            },
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertTrue(filtered.json()["items"])
        self.assertTrue(all(item["channel_id"] == 101 and item["media_type"] == "text" for item in filtered.json()["items"]))

        message_id = filtered.json()["items"][0]["id"]
        bookmark = await self.request("POST", f"/api/messages/{message_id}/bookmark", json={"bookmarked": True})
        self.assertEqual(bookmark.status_code, 200, bookmark.text)
        bookmarked = await self.request("POST", "/api/search", json={"bookmarked_only": True, "limit": 20})
        self.assertEqual(bookmarked.status_code, 200, bookmarked.text)
        self.assertEqual({item["id"] for item in bookmarked.json()["items"]}, {message_id})

    async def test_search_supports_explicit_time_sort_order(self) -> None:
        await self.seed_archive()
        newest = await self.request("POST", "/api/search", json={"query": "", "limit": 20, "sort_order": "desc"})
        oldest = await self.request("POST", "/api/search", json={"query": "", "limit": 20, "sort_order": "asc"})
        self.assertEqual(newest.status_code, 200, newest.text)
        self.assertEqual(oldest.status_code, 200, oldest.text)
        self.assertGreater(newest.json()["items"][0]["sent_at"], newest.json()["items"][-1]["sent_at"])
        self.assertLess(oldest.json()["items"][0]["sent_at"], oldest.json()["items"][-1]["sent_at"])

    async def test_search_preserves_contiguous_chinese_latin_body_text(self) -> None:
        self.telegram.messages[101].append(
            {
                "telegram_id": 98,
                "sent_at": "2026-08-04T00:00:00+00:00",
                "text": "正文含有奈奈的酒馆AI，后面还有补充说明。",
                "media_type": "text",
            }
        )
        await self.seed_archive()
        response = await self.request("POST", "/api/search", json={"query": "奈奈的酒馆AI", "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1, response.json())
        self.assertEqual(response.json()["items"][0]["telegram_id"], 98)

    async def test_whitespace_separated_terms_match_independent_body_fragments(self) -> None:
        self.telegram.messages[101].append(
            {
                "telegram_id": 97,
                "sent_at": "2026-08-04T00:00:00+00:00",
                "text": "音声作品，包含近距离耳语和舔耳片段。",
                "media_type": "audio",
            }
        )
        await self.seed_archive()
        store = self.application.state.service.store
        # Exercise the CJK literal fallback used when FTS has no usable token match.
        with patch.object(store, "_fts_expression", return_value='"fixture-missing-token"'):
            response = await self.request("POST", "/api/search", json={"query": "音声 舔耳", "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1, response.json())
        self.assertEqual(response.json()["items"][0]["telegram_id"], 97)

    async def test_advanced_search_supports_exact_phrases_and_boolean_operators(self) -> None:
        self.telegram.messages[101].extend([
            {
                "telegram_id": 95,
                "sent_at": "2026-08-04T01:00:00+00:00",
                "text": "⭐⭐⭐⭐⭐ ntl 推荐",
                "media_type": "text",
            },
            {
                "telegram_id": 96,
                "sent_at": "2026-08-04T02:00:00+00:00",
                "text": "⭐⭐⭐ ntl 推荐",
                "media_type": "text",
            },
            {
                "telegram_id": 94,
                "sent_at": "2026-08-04T03:00:00+00:00",
                "text": "⭐⭐⭐⭐⭐ 其它内容",
                "media_type": "text",
            },
            {
                "telegram_id": 93,
                "sent_at": "2026-08-04T04:00:00+00:00",
                "text": "⭐⭐⭐⭐ #SLG",
                "media_type": "text",
            },
            {
                "telegram_id": 92,
                "sent_at": "2026-08-04T05:00:00+00:00",
                "text": "⭐️⭐️⭐️⭐️⭐️ #SLG",
                "media_type": "text",
            },
        ])
        await self.seed_archive()

        exact = await self.request("POST", "/api/search", json={"query": '"⭐⭐⭐⭐⭐ ntl"', "limit": 20})
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual([item["telegram_id"] for item in exact.json()["items"]], [95])

        boolean = await self.request("POST", "/api/search", json={"query": '"⭐⭐⭐⭐⭐" AND ntl', "limit": 20})
        self.assertEqual(boolean.status_code, 200, boolean.text)
        self.assertEqual({item["telegram_id"] for item in boolean.json()["items"]}, {95})

        either = await self.request("POST", "/api/search", json={"query": '"⭐⭐⭐⭐⭐ ntl" OR "⭐⭐⭐⭐⭐ 其它内容"', "limit": 20})
        self.assertEqual(either.status_code, 200, either.text)
        self.assertEqual({item["telegram_id"] for item in either.json()["items"]}, {94, 95})

        bounded_stars = await self.request("POST", "/api/search", json={"query": "⭐⭐⭐⭐ & #SLG", "limit": 20})
        self.assertEqual(bounded_stars.status_code, 200, bounded_stars.text)
        self.assertEqual({item["telegram_id"] for item in bounded_stars.json()["items"]}, {93})

    async def test_ad_filters_hide_normalized_duplicates_and_user_keywords_without_deleting_archive(self) -> None:
        self.telegram.messages[101].extend([
            {"telegram_id": 20, "sent_at": "2026-08-08T00:00:00+00:00", "text": "Visit https://example.com/ad?source=one", "media_type": "text"},
            {"telegram_id": 21, "sent_at": "2026-08-09T00:00:00+00:00", "text": "visit https://example.com/ad?source=two", "media_type": "text"},
            {"telegram_id": 22, "sent_at": "2026-08-10T00:00:00+00:00", "text": "PROMO_CODE fixture", "media_type": "text"},
        ])
        await self.seed_archive()
        rules = await self.request("PUT", "/api/ad-filters", json={"keywords": ["promo_code"]})
        self.assertEqual(rules.status_code, 200, rules.text)
        hidden = await self.request("POST", "/api/search", json={"query": "", "limit": 100})
        self.assertEqual(hidden.status_code, 200, hidden.text)
        self.assertGreaterEqual(hidden.json()["hidden_ad_count"], 2)
        self.assertNotIn("PROMO_CODE fixture", {item["text"] for item in hidden.json()["items"]})
        shown = await self.request("POST", "/api/search", json={"query": "", "limit": 100, "include_hidden_ads": True})
        self.assertIn("PROMO_CODE fixture", {item["text"] for item in shown.json()["items"]})
        self.assertEqual((await self.request("GET", "/api/ad-filters")).json()["keywords"], ["promo_code"])

    async def test_image_preview_is_fetched_only_for_the_requested_image_message(self) -> None:
        await self.seed_archive()
        images = await self.request("POST", "/api/search", json={"media_types": ["image"], "limit": 20})
        self.assertEqual(images.status_code, 200, images.text)
        image = images.json()["items"][0]
        preview = await self.request("GET", f"/api/messages/{image['id']}/preview")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.headers["content-type"], "image/png")
        self.assertEqual(preview.headers["cache-control"], "no-store")
        self.assertEqual(self.telegram.preview_calls, [(image["channel_id"], image["telegram_id"])])

    async def test_video_thumbnail_never_requests_the_video_payload(self) -> None:
        self.telegram.messages[101].append(
            {"telegram_id": 99, "sent_at": "2026-08-04T00:00:00+00:00", "text": "#video fixture", "media_type": "video"}
        )
        await self.seed_archive()
        videos = await self.request("POST", "/api/search", json={"media_types": ["video"], "limit": 20})
        self.assertEqual(videos.status_code, 200, videos.text)
        video = videos.json()["items"][0]
        thumbnail = await self.request("GET", f"/api/messages/{video['id']}/thumbnail")
        self.assertEqual(thumbnail.status_code, 200, thumbnail.text)
        self.assertEqual(thumbnail.headers["content-type"], "image/png")
        self.assertEqual(self.telegram.preview_calls, [(video["channel_id"], video["telegram_id"])])

    async def test_incremental_sync_updates_existing_message_without_duplication(self) -> None:
        await self.scan()
        await self.sync([101], None)
        first = await self.request("POST", "/api/search", json={"query": "BODY_SHOULD_NEVER_REACH_AI", "limit": 20})
        self.assertEqual(first.status_code, 200, first.text)
        message_id = first.json()["items"][0]["id"]

        self.telegram.messages[101][0]["text"] = "#alpha #beta shared INCREMENTAL_UPDATE_MARKER"
        verification = await self.request("POST", "/api/channels/101/verify", json={})
        self.assertEqual(verification.status_code, 202, verification.text)
        await self.wait_for_completed_sync(self.client, _job_id(verification.json()))
        updated = await self.request("POST", "/api/search", json={"query": "INCREMENTAL_UPDATE_MARKER", "limit": 20})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["total"], 1)
        self.assertEqual(updated.json()["items"][0]["id"], message_id)
        old = await self.request("POST", "/api/search", json={"query": "BODY_SHOULD_NEVER_REACH_AI", "limit": 20})
        self.assertEqual(old.status_code, 200, old.text)
        self.assertEqual(old.json()["total"], 0)

    async def test_completed_full_history_channel_uses_cursor_for_incremental_updates(self) -> None:
        await self.scan()
        await self.sync([101], None)
        channels = await self.request("GET", "/api/channels")
        channel = next(item for item in channels.json() if item["id"] == 101)
        self.assertTrue(channel["full_history_synced"])
        self.assertTrue(channel["auto_update_enabled"])
        self.assertEqual(channel["last_telegram_id"], 4)

        self.telegram.messages[101].append({
            "telegram_id": 9, "sent_at": "2026-08-05T00:00:00+00:00", "text": "#alpha incremental-only-message", "media_type": "text",
        })
        started = await self.request("POST", "/api/sync/incremental", json={})
        self.assertEqual(started.status_code, 202, started.text)
        self.assertEqual(started.json()["mode"], "incremental")
        completed = await self.wait_for_completed_sync(self.client, _job_id(started.json()))
        self.assertEqual(completed["mode"], "incremental")
        self.assertEqual(completed["messages_processed"], 1)
        fresh = await self.request("POST", "/api/search", json={"query": "incremental-only-message", "limit": 20})
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(fresh.json()["total"], 1)

    async def test_partial_history_sync_stays_selectable_and_does_not_enable_incremental_updates(self) -> None:
        await self.scan()
        await self.sync([101], 2)
        channels = await self.request("GET", "/api/channels")
        channel = next(item for item in channels.json() if item["id"] == 101)
        self.assertFalse(channel["full_history_synced"])
        self.assertFalse(channel["auto_update_enabled"])
        self.assertIsNone(await self.application.state.service.create_incremental_sync())

    async def test_repeated_sync_prioritizes_new_messages_then_backfills_with_remaining_limit(self) -> None:
        await self.scan()
        dialog = self.telegram.dialogs[0]
        # This fixture represents a prior partial archive containing IDs 3 and 4.
        self.application.state.service.store.upsert_messages(dialog, TEST_MESSAGES[101][2:])
        self.telegram.messages[101].append({
            "telegram_id": 5, "sent_at": "2026-08-07T00:00:00+00:00", "text": "#alpha newest", "media_type": "text",
        })

        repeated = await self.sync([101], 2)
        self.assertEqual(repeated["results"][0]["strategy"], "new-and-backfill")
        self.assertEqual(self.telegram.fetch_windows[-2:], [(101, 2, 4, 0), (101, 1, 0, 3)])
        channel = next(item for item in (await self.request("GET", "/api/channels")).json() if item["id"] == 101)
        self.assertEqual(channel["message_count"], 4)

    async def test_sync_records_local_message_id_order_check(self) -> None:
        await self.scan()
        job = await self.sync([101], 2)
        integrity = job["results"][0]["integrity"]
        self.assertTrue(integrity["verified"])
        self.assertEqual(integrity["message_count"], 2)
        self.assertEqual((integrity["first_id"], integrity["last_id"]), (1, 2))

    async def test_manual_verification_completes_history_and_enables_auto_update(self) -> None:
        await self.scan()
        await self.sync([101], 2)
        verified = await self.request("POST", "/api/channels/101/verify", json={})
        self.assertEqual(verified.status_code, 202, verified.text)
        self.assertEqual(verified.json()["mode"], "verify")
        completed = await self.wait_for_completed_sync(self.client, _job_id(verified.json()))
        self.assertEqual(completed["messages_processed"], 4)
        reconciliation = completed["results"][0]["verification"]
        self.assertTrue(reconciliation["verified"])
        self.assertEqual(reconciliation["local_before_count"], 2)
        self.assertEqual(reconciliation["remote_visible_count"], 4)
        self.assertEqual(reconciliation["restored_count"], 2)
        self.assertEqual(reconciliation["local_only_count"], 0)
        channels = await self.request("GET", "/api/channels")
        channel = next(item for item in channels.json() if item["id"] == 101)
        self.assertEqual(channel["message_count"], 4)
        self.assertTrue(channel["full_history_synced"])
        self.assertTrue(channel["auto_update_enabled"])
        self.assertEqual(channel["last_telegram_id"], 4)
        self.telegram.messages[101].append({
            "telegram_id": 8, "sent_at": "2026-08-06T00:00:00+00:00", "text": "#alpha verified-incremental", "media_type": "text",
        })
        incremental = await self.application.state.service.create_incremental_sync()
        self.assertIsNotNone(incremental)
        await self.wait_for_completed_sync(self.client, incremental["job_id"])
        self.assertEqual(self.telegram.fetch_calls[-1], (101, 10000))

    async def test_service_start_schedules_incremental_sync_for_completed_history(self) -> None:
        await self.scan()
        await self.sync([101], None)
        self.telegram.messages[101].append({
            "telegram_id": 8, "sent_at": "2026-08-06T00:00:00+00:00", "text": "#alpha startup-incremental", "media_type": "text",
        })
        await self.application.state.service.start()
        for _ in range(100):
            jobs = await self.request("GET", "/api/sync")
            incremental = next((item for item in jobs.json()["items"] if item.get("mode") == "incremental"), None)
            if incremental and incremental.get("state") in {"completed", "failed"}:
                self.assertEqual(incremental["state"], "completed", jobs.text)
                self.assertEqual(incremental["messages_processed"], 1)
                break
            await asyncio.sleep(0.005)
        else:
            self.fail("startup incremental sync did not complete")

    async def test_delete_cascades_fts_bookmarks_and_tags_but_preserves_other_channel(self) -> None:
        await self.seed_archive()
        first = await self.request("POST", "/api/search", json={"query": "shared", "limit": 20})
        message_id = first.json()["items"][0]["id"]
        await self.request("POST", f"/api/messages/{message_id}/bookmark", json={"bookmarked": True})

        deleted = await self.request("DELETE", "/api/channels/101")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        body = deleted.json()
        self.assertEqual(body["channel_id"], 101)
        self.assertGreaterEqual(body["messages_deleted"], 1)
        self.assertGreaterEqual(body["bookmarks_deleted"], 1)

        remaining = await self.request("POST", "/api/search", json={"query": "group-only", "limit": 20})
        self.assertEqual(remaining.status_code, 200, remaining.text)
        self.assertEqual(remaining.json()["total"], 1)
        removed = await self.request("POST", "/api/search", json={"query": "shared", "limit": 20})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["total"], 0)
        tags = await self.request("GET", "/api/tags")
        self.assertEqual(tags.status_code, 200, tags.text)
        self.assertNotIn("#alpha", _tag_names(tags.json()))
        self.assertIn("#gamma", _tag_names(tags.json()))

    async def test_delete_is_blocked_while_sync_is_running(self) -> None:
        await self.scan()
        self.telegram.block_sync = True
        start = await self.request("POST", "/api/sync", json={"channel_ids": [101], "limit_per_channel": None})
        self.assertIn(start.status_code, (200, 202), start.text)
        await asyncio.wait_for(self.telegram.started.wait(), timeout=1)

        second = await self.request("POST", "/api/sync", json={"channel_ids": [101], "limit_per_channel": None})
        self.assertIn(second.status_code, (200, 202), second.text)
        self.assertEqual(_job_id(second.json()), _job_id(start.json()))
        delete = await self.request("DELETE", "/api/channels/101")
        self.assertEqual(delete.status_code, 409, delete.text)

        self.telegram.release.set()
        job_id = _job_id(start.json())
        for _ in range(100):
            status = await self.request("GET", f"/api/sync/{job_id}")
            if _job(status.json()).get("state") == "completed":
                break
            await asyncio.sleep(0.005)
        else:
            self.fail("blocked sync did not finish after release")

    async def test_sync_jobs_run_in_fifo_sequence(self) -> None:
        await self.scan()
        self.telegram.block_sync = True
        first = await self.request("POST", "/api/sync", json={"channel_ids": [101], "limit_per_channel": None})
        self.assertIn(first.status_code, (200, 202), first.text)
        await asyncio.wait_for(self.telegram.started.wait(), timeout=1)

        second = await self.request("POST", "/api/sync", json={"channel_ids": [102], "limit_per_channel": None})
        self.assertIn(second.status_code, (200, 202), second.text)
        first_id, second_id = _job_id(first.json()), _job_id(second.json())
        first_status = await self.request("GET", f"/api/sync/{first_id}")
        second_status = await self.request("GET", f"/api/sync/{second_id}")
        self.assertEqual(_job(first_status.json())["state"], "running")
        self.assertEqual(_job(second_status.json())["state"], "queued")
        self.assertEqual(self.telegram.fetch_calls, [(101, None)])

        self.telegram.release.set()
        completed_first = await self.wait_for_completed_sync(self.client, first_id)
        completed_second = await self.wait_for_completed_sync(self.client, second_id)
        self.assertEqual(completed_first["state"], "completed")
        self.assertEqual(completed_second["state"], "completed")
        self.assertEqual(self.telegram.fetch_calls, [(101, None), (102, None)])

    async def test_delete_sync_job_cancels_running_task_and_keeps_archive(self) -> None:
        await self.scan()
        self.telegram.block_sync = True
        start = await self.request("POST", "/api/sync", json={"channel_ids": [101], "limit_per_channel": None})
        self.assertIn(start.status_code, (200, 202), start.text)
        job_id = _job_id(start.json())
        await asyncio.wait_for(self.telegram.started.wait(), timeout=1)

        deleted = await self.request("DELETE", f"/api/sync/{job_id}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"job_id": job_id, "deleted": True})
        missing = await self.request("GET", f"/api/sync/{job_id}")
        self.assertEqual(missing.status_code, 404, missing.text)
        jobs = await self.request("GET", "/api/sync")
        self.assertEqual(jobs.status_code, 200, jobs.text)
        self.assertFalse(any(_job(item).get("job_id") == job_id for item in _items(jobs.json())))

    async def test_blocked_running_sync_recovers_on_lifespan_without_duplicate_rows(self) -> None:
        await self.scan()
        self.telegram.block_sync = True
        start = await self.request(
            "POST",
            "/api/sync",
            json={"channel_ids": [101], "limit_per_channel": None},
        )
        self.assertIn(start.status_code, (200, 202), start.text)
        job_id = _job_id(start.json())
        await asyncio.wait_for(self.telegram.started.wait(), timeout=1)

        for _ in range(100):
            status_response = await self.request("GET", f"/api/sync/{job_id}")
            self.assertEqual(status_response.status_code, 200, status_response.text)
            if _job(status_response.json()).get("state") == "running":
                break
            await asyncio.sleep(0.005)
        else:
            self.fail("blocked sync never reached running state")

        await self.application.state.service.stop()
        await self.client.aclose()
        with closing(sqlite3.connect(self.storage / "telescope.db")) as db:
            self.assertEqual(
                db.execute("SELECT state FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()[0],
                "queued",
            )
            db.execute("UPDATE sync_jobs SET state='running' WHERE job_id=?", (job_id,))
            db.commit()

        restarted_telegram = FakeTelegramGateway()
        restarted_telegram.block_sync = True
        self.addCleanup(restarted_telegram.release.set)
        restarted_application = _create_application(self.storage, restarted_telegram, RecordingAIGateway())
        async with restarted_application.router.lifespan_context(restarted_application):
            transport = httpx.ASGITransport(app=restarted_application)
            async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as restarted_client:
                for _ in range(100):
                    status_response = await restarted_client.get(f"/api/sync/{job_id}")
                    self.assertEqual(status_response.status_code, 200, status_response.text)
                    status = _job(status_response.json())
                    if status.get("state") == "running":
                        break
                    await asyncio.sleep(0.005)
                else:
                    self.fail("recovered sync did not reach the blocked running state during lifespan")

                await asyncio.wait_for(restarted_telegram.started.wait(), timeout=1)

                duplicate = await restarted_client.post(
                    "/api/sync",
                    json={"channel_ids": [101], "limit_per_channel": None},
                )
                self.assertIn(duplicate.status_code, (200, 202), duplicate.text)
                self.assertEqual(_job_id(duplicate.json()), job_id)
                await asyncio.sleep(0)
                self.assertEqual(restarted_telegram.fetch_calls, [(101, None)])

                restarted_telegram.release.set()
                await self.wait_for_completed_sync(restarted_client, job_id)

                with closing(sqlite3.connect(self.storage / "telescope.db")) as db:
                    row = db.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT telegram_id) FROM messages WHERE channel_id=?",
                        (101,),
                    ).fetchone()
                self.assertEqual(row, (len(TEST_MESSAGES[101]), len(TEST_MESSAGES[101])))

                incremental = await restarted_client.post(
                    "/api/sync",
                    json={"channel_ids": [101], "limit_per_channel": None},
                )
                self.assertIn(incremental.status_code, (200, 202), incremental.text)
                incremental_job_id = _job_id(incremental.json())
                self.assertNotEqual(incremental_job_id, job_id)
                await self.wait_for_completed_sync(restarted_client, incremental_job_id)
                self.assertEqual(restarted_telegram.fetch_calls, [(101, None), (101, None)])

                with closing(sqlite3.connect(self.storage / "telescope.db")) as db:
                    row = db.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT telegram_id) FROM messages WHERE channel_id=?",
                        (101,),
                    ).fetchone()
                self.assertEqual(row, (len(TEST_MESSAGES[101]), len(TEST_MESSAGES[101])))

    async def test_tag_dictionary_has_count_version_idempotence_and_delete_updates(self) -> None:
        await self.seed_archive()
        first = await self.request("GET", "/api/tags")
        second = await self.request("GET", "/api/tags")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        first_body, second_body = first.json(), second.json()
        first_version = _value(first_body, "dictionary_version", "version")
        self.assertIsInstance(first_version, str)
        counts = _value(first_body, "counts", default={})
        self.assertIsInstance(counts, dict)
        self.assertEqual(set(counts), set(_tag_names(first_body)))
        self.assertEqual(
            {tag: counts[tag] for tag in ("#alpha", "#beta", "#gamma", "#album")},
            {"#alpha": 2, "#beta": 1, "#gamma": 1, "#album": 2},
        )
        self.assertEqual(first_body, second_body)
        self.assertTrue({"#alpha", "#beta", "#gamma"}.issubset(_tag_names(first_body)))

        await self.request("DELETE", "/api/channels/101")
        after_delete = await self.request("GET", "/api/tags")
        self.assertNotEqual(
            _value(after_delete.json(), "dictionary_version", "version"),
            first_version,
        )
        self.assertNotIn("#alpha", _tag_names(after_delete.json()))
        self.assertIn("#gamma", _tag_names(after_delete.json()))

    async def test_ai_search_sends_only_query_and_complete_dictionary_and_whitelists_output(self) -> None:
        await self.seed_archive()
        response = await self.request(
            "POST",
            "/api/ai/search",
            json={
                "query": "find shared alpha",
                "minimum_results": 1,
                "channel_ids": [101],
                "media_types": ["text"],
                "from_date": "2026-08-01",
                "until_date": "2026-08-02",
                "limit": 20,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.ai.requests)
        serialized = json.dumps(self.ai.requests, ensure_ascii=False)
        for marker in (
            "BODY_SHOULD_NEVER_REACH_AI",
            "only text",
            "album caption token",
            "group-only text",
            "sender-a",
            "sender-b",
            "sender-c",
            "sender-d",
        ):
            self.assertNotIn(marker, serialized)
        for tag in ("#alpha", "#beta", "#gamma", "#album"):
            self.assertIn(tag, serialized)

        interpretation = _interpretation(response.json())
        selected = _value(interpretation, "selected_tags", "matched_tags", "ranked_tags", default=[])
        self.assertIsInstance(selected, list)
        self.assertTrue(set(selected).issubset({"#alpha", "#beta", "#gamma", "#album"}))
        self.assertNotIn("#made-up", selected)
        self.assertTrue(all(item["channel_id"] == 101 and item["media_type"] == "text" for item in response.json()["items"]))

    async def test_ai_provider_keywords_cannot_constrain_fts_results(self) -> None:
        await self.seed_archive()
        self.ai.tag_response = {"tags": ["#alpha", "#beta"], "keywords": ["NEVER_MATCH"]}
        response = await self.request(
            "POST",
            "/api/ai/search",
            json={
                "query": "shared",
                "minimum_results": 1,
                "channel_ids": [101],
                "media_types": ["text"],
                "from_date": "2026-08-01",
                "until_date": "2026-08-02",
                "limit": 20,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertGreater(body["total"], 0, body)
        self.assertTrue(body["items"], body)
        self.assertNotIn("NEVER_MATCH", json.dumps(body), body)
        interpretation = _interpretation(body)
        self.assertEqual(interpretation["effective_tags"], ["#alpha", "#beta"])
        self.assertTrue(
            set(interpretation["ranked_tags"]).issubset({"#alpha", "#beta", "#gamma", "#album"}),
            interpretation,
        )
        self.assertTrue(
            all(
                item["channel_id"] == 101
                and item["media_type"] == "text"
                and {"#alpha", "#beta"}.issubset(_message_tags(item))
                for item in body["items"]
            ),
            body,
        )

    async def test_ai_search_uses_selected_tags_when_the_natural_phrase_is_not_literal_archive_text(self) -> None:
        await self.seed_archive()
        self.ai.tag_response = {"ranked_tags": ["#alpha"]}
        response = await self.request(
            "POST",
            "/api/ai/search",
            json={"query": "a semantic request that is absent from every archived message", "minimum_results": 1, "limit": 20},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()["total"], 0, response.json())
        self.assertEqual(_interpretation(response.json())["effective_tags"], ["#alpha"])
        self.assertTrue(all("#alpha" in _message_tags(item) for item in response.json()["items"]))

    async def test_ai_search_ranks_tag_candidates_by_natural_language_relevance(self) -> None:
        await self.seed_archive()
        self.ai.tag_response = {"ranked_tags": ["#alpha"]}
        response = await self.request(
            "POST", "/api/ai/search", json={"query": "shared", "minimum_results": 1, "limit": 20}
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertGreater(len(items), 1, items)
        self.assertIn("shared", items[0]["text"])
        self.assertGreater(items[0]["relevance_score"], items[-1]["relevance_score"])

    async def test_ai_relaxation_is_ordered_counted_and_stops_at_threshold(self) -> None:
        self.telegram.messages[101].extend(
            [
                {
                    "telegram_id": 5,
                    "sent_at": "2026-08-01T00:30:00+00:00",
                    "text": "#alpha eligible second",
                    "media_type": "text",
                    "sender": "eligible-second",
                },
                {
                    "telegram_id": 6,
                    "sent_at": "2026-08-03T00:00:00+00:00",
                    "text": "#alpha date decoy",
                    "media_type": "text",
                    "sender": "date-decoy",
                },
                {
                    "telegram_id": 7,
                    "sent_at": "2026-08-01T00:45:00+00:00",
                    "text": "#alpha media decoy",
                    "media_type": "image",
                    "sender": "media-decoy",
                },
            ]
        )
        self.telegram.messages[102].append(
            {
                "telegram_id": 2,
                "sent_at": "2026-08-01T05:00:00+00:00",
                "text": "#alpha channel decoy",
                "media_type": "text",
                "sender": "channel-decoy",
            }
        )
        await self.seed_archive()
        alpha_messages = await self.request("POST", "/api/search", json={"query": "#alpha", "channel_ids": [101], "limit": 100})
        self.assertEqual(alpha_messages.status_code, 200, alpha_messages.text)
        bookmark_ids = {
            item["id"]
            for item in alpha_messages.json()["items"]
            if item["telegram_id"] in {1, 5}
        }
        self.assertEqual(len(bookmark_ids), 2)
        for message_id in bookmark_ids:
            bookmarked = await self.request(
                "POST",
                f"/api/messages/{message_id}/bookmark",
                json={"bookmarked": True},
            )
            self.assertEqual(bookmarked.status_code, 200, bookmarked.text)

        filters = {
            "channel_ids": [101],
            "media_types": ["text"],
            "from_date": "2026-08-01",
            "until_date": "2026-08-02",
            "bookmarked_only": True,
            "limit": 1,
            "offset": 1,
        }
        response = await self.request(
            "POST",
            "/api/ai/search",
            json={"query": "#alpha #beta #gamma", "minimum_results": 2, **filters},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()["total"], 0)
        self.assertTrue(response.json()["items"])
        interpretation = _interpretation(response.json())
        trace = _trace(response.json())
        self.assertEqual(len(trace), 2)
        relaxation = response.json()["relaxation"]
        self.assertEqual(relaxation["initial_count"], 0)
        self.assertEqual(relaxation["threshold"], 2)
        counts = [_trace_count(step) for step in trace]
        self.assertEqual(counts, [1, 2])
        self.assertEqual([_trace_removed(step) for step in trace], ["#gamma", "#beta"])
        self.assertEqual(_trace_tags(trace[0]), ["#alpha", "#beta"])
        self.assertEqual(_trace_tags(trace[1]), ["#alpha"])
        self.assertEqual(relaxation["final_count"], 2)
        self.assertEqual(interpretation["effective_tags"], ["#alpha"])

        ordinary_payload = {
            "query": "",
            "selected_tags": interpretation["effective_tags"],
            **filters,
        }
        ordinary = await self.request(
            "POST",
            "/api/search",
            json=ordinary_payload,
        )
        self.assertEqual(ordinary.status_code, 200, ordinary.text)
        self.assertEqual(ordinary.json()["total"], 2)
        self.assertEqual([item["id"] for item in response.json()["items"]], [item["id"] for item in ordinary.json()["items"]])
        self.assertEqual(response.json()["total"], ordinary.json()["total"])
        item = response.json()["items"][0]
        self.assertEqual(item["telegram_id"], 1)
        self.assertEqual(item["channel_id"], 101)
        self.assertEqual(item["media_type"], "text")
        self.assertTrue(item["bookmarked"])
        self.assertTrue(item["sent_at"].startswith("2026-08-01"))

    async def test_settings_model_discovery_cache_timeout_and_hybrid_fallback(self) -> None:
        settings = await self.request(
            "POST",
            "/api/settings",
            json={
                "api_id": 123,
                "api_hash": "FAKE_HASH",
                "phone": "+00000000000",
                "proxy_type": "socks5",
                "proxy_host": "127.0.0.1",
                "proxy_port": 7890,
            },
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        saved = await self.request("GET", "/api/settings")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["api_id"], 123)

        ai_settings = await self.request(
            "PUT",
            "/api/ai/settings",
            json={
                "mode": "hybrid",
                "local_base_url": "http://fake.local",
                "local_model": "local-fixture-model",
                "cloud_base_url": "https://fake.cloud/v1",
                "cloud_model": "cloud-fixture-model",
                "cloud_api_key": "FAKE_KEY",
            },
        )
        self.assertEqual(ai_settings.status_code, 200, ai_settings.text)
        ai_settings_readback = await self.request("GET", "/api/ai/settings")
        self.assertEqual(ai_settings_readback.status_code, 200, ai_settings_readback.text)
        self.assertEqual(ai_settings_readback.json()["mode"], "hybrid")

        models = await self.request(
            "POST",
            "/api/ai/models",
            json={"provider": "cloud", "base_url": "https://fake.cloud/v1", "api_key": "FAKE_KEY"},
        )
        cached = await self.request(
            "POST",
            "/api/ai/models",
            json={"provider": "cloud", "base_url": "https://fake.cloud/v1", "api_key": "FAKE_KEY"},
        )
        self.assertEqual(models.status_code, 200, models.text)
        self.assertEqual(cached.status_code, 200, cached.text)
        self.assertEqual(models.json(), cached.json())
        self.assertEqual(len(self.ai.model_calls), 1)

        self.ai.local_failure = True
        connection = await self.request("POST", "/api/ai/test")
        self.assertEqual(connection.status_code, 200, connection.text)
        self.assertEqual(_value(connection.json(), "provider", "backend"), "cloud")

        current_form_connection = await self.request(
            "POST", "/api/ai/test",
            json={"provider": "cloud", "base_url": "https://fake.cloud/v1", "model": "cloud-fixture-model", "api_key": "FAKE_KEY"},
        )
        self.assertEqual(current_form_connection.status_code, 200, current_form_connection.text)
        self.assertEqual(current_form_connection.json()["model"], "cloud-fixture-model")

        self.ai.timeout = True
        timeout = await self.request(
            "POST",
            "/api/ai/models",
            json={"provider": "local", "base_url": "http://fake.local", "api_key": ""},
        )
        self.assertIn(timeout.status_code, (408, 504, 503), timeout.text)

    async def test_concurrent_identical_injected_model_discovery_is_coalesced(self) -> None:
        self.ai.block_models = True
        payload = {"provider": "cloud", "base_url": "https://fake.cloud/v1", "api_key": "FAKE_KEY"}
        leader = asyncio.create_task(self.request("POST", "/api/ai/models", json=payload))
        follower: asyncio.Task[httpx.Response] | None = None
        try:
            await asyncio.wait_for(self.ai.models_started.wait(), timeout=1)
            follower = asyncio.create_task(self.request("POST", "/api/ai/models", json=payload))
            await asyncio.sleep(0)
            self.assertEqual(self.ai.model_calls, [payload])
        finally:
            self.ai.models_release.set()
            responses = await asyncio.gather(leader, *([follower] if follower is not None else []), return_exceptions=True)

        self.assertTrue(all(isinstance(response, httpx.Response) for response in responses), responses)
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual([response.json() for response in responses], [{"models": ["cloud-fixture-model"]}] * 2)

    async def test_sync_and_archive_survive_factory_recreation(self) -> None:
        await self.seed_archive()
        search = await self.request("POST", "/api/search", json={"query": "shared", "limit": 20})
        message_id = search.json()["items"][0]["id"]
        bookmark = await self.request("POST", f"/api/messages/{message_id}/bookmark", json={"bookmarked": True})
        self.assertEqual(bookmark.status_code, 200, bookmark.text)
        settings = await self.request(
            "POST",
            "/api/settings",
            json={"api_id": 321, "api_hash": "FAKE_HASH", "phone": "+00000000000"},
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        job_index = await self.request("GET", "/api/sync")
        self.assertEqual(job_index.status_code, 200, job_index.text)
        self.assertTrue(_items(job_index.json()))
        self.assertTrue(any(item.get("state") == "completed" for item in _items(job_index.json())))

        await self.application.state.service.stop()
        await self.client.aclose()
        self.application = _create_application(self.storage, FakeTelegramGateway(), RecordingAIGateway())
        self.transport = httpx.ASGITransport(app=self.application)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://fixture")

        channels = await self.request("GET", "/api/channels")
        self.assertEqual(channels.status_code, 200, channels.text)
        self.assertEqual({item["id"] for item in _items(channels.json())}, {101, 102})
        restored = await self.request("POST", "/api/search", json={"bookmarked_only": True, "limit": 20})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual({item["id"] for item in restored.json()["items"]}, {message_id})
        restored_jobs = await self.request("GET", "/api/sync")
        self.assertEqual(restored_jobs.status_code, 200, restored_jobs.text)
        self.assertTrue(any(item.get("state") == "completed" for item in _items(restored_jobs.json())))
        tags = await self.request("GET", "/api/tags")
        self.assertIn("#alpha", _tag_names(tags.json()))
        config = await self.request("GET", "/api/settings")
        self.assertEqual(config.json()["api_id"], 321)


class MigrationTests(unittest.TestCase):
    def test_obsolete_ai_tables_are_removed_without_losing_archive_fts_bookmarks_scan_or_tags(self) -> None:
        with TemporaryDirectory(prefix="telescope-migration-", dir=TEST_TMP_ROOT) as directory:
            storage = Path(directory)
            db_path = storage / "telescope.db"
            db = sqlite3.connect(db_path)
            try:
                db.executescript(
                    """
                    CREATE TABLE channels (id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT, kind TEXT NOT NULL, synced_at TEXT, message_count INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL, sent_at TEXT NOT NULL, sender TEXT, text TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT 'text', has_link INTEGER NOT NULL DEFAULT 0, has_tag INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '', permalink TEXT, reply_to INTEGER, grouped_id TEXT, album_text TEXT NOT NULL DEFAULT '');
                    CREATE VIRTUAL TABLE message_search USING fts5(text, tags, content='messages', content_rowid='id');
                    CREATE TABLE bookmarks (message_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
                    CREATE TABLE scanned_dialogs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT, kind TEXT NOT NULL, scanned_at TEXT NOT NULL);
                    CREATE TABLE local_tags (tag TEXT PRIMARY KEY, message_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
                    CREATE TABLE ai_documents (message_id INTEGER PRIMARY KEY, summary TEXT, embedding TEXT);
                    CREATE TABLE ai_index_jobs (job_id TEXT PRIMARY KEY, state TEXT NOT NULL);
                    INSERT INTO channels VALUES (7001, 'Migrated', 'migrated', 'channel', NULL, 1);
                    INSERT INTO messages VALUES (1, 7001, 1, '2026-08-01T00:00:00+00:00', 'fixture', 'migration_token #kept', 'text', 0, 1, '#kept', 'https://t.me/migrated/1', NULL, NULL, '');
                    INSERT INTO message_search(rowid, text, tags) VALUES (1, 'migration_token #kept', '#kept');
                    INSERT INTO bookmarks VALUES (1, '2026-08-01T00:00:00+00:00');
                    INSERT INTO scanned_dialogs VALUES (7001, 'Migrated', 'migrated', 'channel', '2026-08-01T00:00:00+00:00');
                    INSERT INTO local_tags VALUES ('#kept', 1, '2026-08-01T00:00:00+00:00');
                    INSERT INTO ai_documents VALUES (1, 'obsolete summary', 'obsolete vector');
                    INSERT INTO ai_index_jobs VALUES ('obsolete-job', 'completed');
                    """
                )
            finally:
                db.close()

            application = _create_application(storage, FakeTelegramGateway(), RecordingAIGateway())
            service = getattr(getattr(application, "state", None), "service", None)
            if service is not None:
                async def initialize_and_stop() -> None:
                    await service.start()
                    await service.stop()

                asyncio.run(initialize_and_stop())
            db = sqlite3.connect(db_path)
            try:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("ai_documents", tables)
                self.assertNotIn("ai_index_jobs", tables)
                self.assertFalse({name for name in tables if name.startswith("ai_")})
                self.assertEqual(db.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM message_search WHERE message_search MATCH 'migration_token'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM scanned_dialogs").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT tag FROM local_tags").fetchone()[0], "#kept")
            finally:
                db.close()


class LauncherContractTests(unittest.TestCase):
    def test_launcher_uses_the_default_port_window_without_starting_a_service(self) -> None:
        launcher = ENTRYPOINT.read_text(encoding="utf-8")
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        batch_launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        self.assertRegex(launcher, r"range\(8000,\s*8011\)")
        self.assertIn("TELESCOPE_PORT", launcher)
        self.assertIn('subprocess.Popen([sys.executable, "server.py"]', launcher)
        self.assertIn('os.environ.get("TELESCOPE_PORT", "8000")', server)
        self.assertIn('set "CONDA_ACTIVATE=%TELESCOPE_CONDA_ROOT%\\condabin\\conda.bat"', batch_launcher)
        self.assertIn('call "%CONDA_ACTIVATE%" activate "%TELESCOPE_CONDA_ENV%"', batch_launcher)
        self.assertIn('set "TELESCOPE_CONDA_ENV=tg-history-search"', batch_launcher)
        self.assertIn("python entry.py", batch_launcher)
        self.assertNotIn("py entry.py", batch_launcher)

    def test_launcher_waits_for_an_http_success_not_a_listening_socket(self) -> None:
        class Process:
            def poll(self) -> None:
                return None

        class Response:
            status = 200

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

        with patch.object(entry.urllib.request, "urlopen", side_effect=[OSError("starting"), Response()]) as request:
            with patch.object(entry.time, "sleep"):
                entry.wait_until_ready(18080, Process())
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:18080/api/status")


FRONTEND_NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const source = input.source;
const newline = String.fromCharCode(10);
const marker = "  boot();" + newline + "})();";
if (!source.includes(marker)) throw new Error("frontend boot marker is missing");

const elements = new Map();
function classList() {
  const values = new Set();
  return {
    add: (...names) => names.forEach((name) => values.add(name)),
    remove: (...names) => names.forEach((name) => values.delete(name)),
    contains: (name) => values.has(name),
    toggle: (name, force) => {
      const next = force === undefined ? !values.has(name) : Boolean(force);
      if (next) values.add(name); else values.delete(name);
      return next;
    },
  };
}
function element(id) {
  const attrs = {};
  const children = new Map();
  const node = {
    id,
    value: "",
    textContent: "",
    innerHTML: "",
    disabled: false,
    open: false,
    dataset: {},
    classList: classList(),
    addEventListener: () => {},
    removeEventListener: () => {},
    setAttribute: (name, value) => { attrs[name] = String(value); },
    getAttribute: (name) => attrs[name] ?? null,
    removeAttribute: (name) => { delete attrs[name]; },
    contains: () => true,
    closest: () => null,
    focus: () => {},
    insertAdjacentHTML: (position, value) => {
      if (position === "beforeend") node.innerHTML += value;
    },
    showModal: () => { node.open = true; },
    close: () => { node.open = false; },
    querySelector: (selector) => {
      if (!children.has(selector)) {
        children.set(selector, {
          value: "",
          textContent: "",
          disabled: false,
          classList: classList(),
          setAttribute: () => {},
          removeAttribute: () => {},
          addEventListener: () => {},
          querySelector: () => null,
        });
      }
      return children.get(selector);
    },
    querySelectorAll: () => [],
  };
  elements.set(id, node);
  return node;
}
function getElement(id) {
  return elements.get(id) || element(id);
}
const document = {
  visibilityState: "visible",
  body: getElement("body"),
  activeElement: null,
  querySelector: (selector) => {
    if (selector.startsWith("#")) {
      const parts = selector.slice(1).split(/\s+/, 2);
      return parts.length === 1 ? getElement(parts[0]) : getElement(parts[0]).querySelector(parts[1]);
    }
    return null;
  },
  querySelectorAll: () => [],
  addEventListener: () => {},
};
const stored = { value: null };
const localStorage = {
  getItem: () => stored.value,
  setItem: (_, value) => { stored.value = value; },
  removeItem: () => { stored.value = null; },
};
const window = {
  location: { origin: "http://fixture" },
  addEventListener: () => {},
  confirm: () => true,
};
const timers = [];
let nextTimer = 0;
let clearCalls = 0;
function schedule(callback, delay) {
  const timer = { id: ++nextTimer, callback, delay, cancelled: false };
  timers.push(timer);
  return timer.id;
}
function cancelTimer(id) {
  clearCalls += 1;
  const timer = timers.find((entry) => entry.id === id);
  if (timer) timer.cancelled = true;
}
const context = {
  document,
  window,
  localStorage,
  navigator: { onLine: true },
  fetch: async () => { throw new Error("fixture fetch handler not installed"); },
  setTimeout: schedule,
  clearTimeout: cancelTimer,
  setInterval: schedule,
  clearInterval: cancelTimer,
  AbortController,
  FormData: class {
    constructor() {}
    entries() { return []; }
  },
  URL,
  Map,
  Set,
  Promise,
  JSON,
  Object,
  Array,
  Number,
  String,
  Boolean,
  Error,
  RegExp,
  Date,
  Math,
  console: { log: () => {}, warn: () => {}, error: () => {} },
};
const instrumented = source.replace(
  marker,
  "  globalThis.__teleScopeExports = { boot, request, normalizeDictionary, renderMessageText, updateTagAnalysis, scheduleSyncPoll, clearSyncTimer, runSearch, renderSyncQueue, deleteSyncTask, state };" + newline + "})();",
);
vm.runInNewContext(instrumented, context, { filename: "static/app.js" });
const app = context.__teleScopeExports;
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
const response = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  text: async () => JSON.stringify(body),
});
const node = (id) => getElement(id);

async function runCase(name) {
  if (name === "counts-map") {
    const value = app.normalizeDictionary({
      tags: ["#alpha", "#beta"],
      counts: { "#alpha": 7, "#beta": 2 },
    });
    assert(JSON.stringify(value) === JSON.stringify([
      { tag: "#alpha", count: 7 },
      { tag: "#beta", count: 2 },
    ]), "dictionary counts map was not applied to tag payload");
    return value;
  }

  if (name === "markdown-message-text") {
    const value = app.renderMessageText("**** ****#道具**** [查看详情](https://fixture.invalid/item)\n\n\n`补充` ~~已失效~~");
    assert(value === '<strong>#道具</strong> <a class="message-inline-link" href="https://fixture.invalid/item" target="_blank" rel="noopener noreferrer">查看详情</a><br><br><code>补充</code> <s>已失效</s>', "message markdown was not rendered safely");
    return value;
  }

  if (name === "nested-relaxation") {
    app.state.tags.dictionary = [{ tag: "#alpha", count: 7 }, { tag: "#beta", count: 2 }];
    app.updateTagAnalysis({
      interpretation: {
        ranked_tags: ["#alpha", "#beta"],
        effective_tags: ["#alpha"],
      },
      relaxation: {
        initial_count: 0,
        final_count: 2,
        threshold: 2,
        steps: [{ removed_tag: "#beta", remaining_tags: ["#alpha"], count: 2 }],
      },
    }, true);
    const step = app.state.tags.trace.find((entry) => entry.removed === "#beta");
    assert(step && step.count === 2 && JSON.stringify(step.tags) === JSON.stringify(["#alpha"]),
      "nested relaxation steps were not rendered from interpretation.relaxation.steps");
    return { trace: app.state.tags.trace, initial: app.state.tags.initialCount, final: app.state.tags.finalCount };
  }

  if (name === "one-timer") {
    app.state.sync.jobId = "fixture-job";
    app.state.sync.timer = null;
    app.scheduleSyncPoll(10);
    app.scheduleSyncPoll(20);
    const active = timers.filter((entry) => !entry.cancelled);
    assert(active.length === 1, "sync polling left more than one active timer");
    assert(active[0].delay === 20 && app.state.sync.timer === active[0].id,
      "sync polling did not retain the newest timer");
    return { active: active.length, clearCalls, delay: active[0].delay };
  }

  if (name === "write-no-retry") {
    const requests = [];
    context.fetch = async (path, options) => {
      requests.push({ path, method: options.method, body: options.body });
      throw new Error("fixture write failure");
    };
    let error = null;
    try {
      await app.request("/api/settings", { method: "POST", body: '{"fixture":true}' });
    } catch (caught) {
      error = caught;
    }
    assert(error && error.kind === "network", "write failure did not surface as a network error");
    assert(requests.length === 1, "write request was retried");
    assert(requests[0].method === "POST" && requests[0].body === '{"fixture":true}',
      "write request payload or method changed");
    return { requests, kind: error.kind };
  }

  if (name === "abort-latest-wins") {
    const requests = [];
    context.fetch = (path, options) => {
      const body = JSON.parse(options.body);
      requests.push({ path, query: body.query, signal: options.signal });
      if (body.query === "old") {
        return new Promise((resolve, reject) => {
          options.signal.addEventListener("abort", () => reject(new Error("old request aborted")), { once: true });
        });
      }
      return Promise.resolve(response({
        items: [{ id: 2, sent_at: "2026-08-01T00:00:00+00:00", text: "newest", media_type: "text", channel_title: "fixture" }],
        total: 1,
      }));
    };
    node("query").value = "old";
    const oldRequest = app.runSearch({ reset: true, explicit: true });
    node("query").value = "new";
    const latestRequest = app.runSearch({ reset: true, explicit: true });
    await Promise.all([oldRequest, latestRequest]);
    assert(requests.length === 2, "search did not issue one request per query");
    assert(requests[0].signal.aborted, "stale search controller was not aborted");
    assert(node("results").innerHTML.includes('data-message-id="2"'), "latest search result was not rendered");
    assert(!node("results").innerHTML.includes('data-message-id="1"'), "stale search result won the race");
    return {
      requests: requests.map(({ path, query }) => ({ path, query })),
      oldAborted: requests[0].signal.aborted,
      results: node("results").innerHTML,
    };
  }

  if (name === "sync-recovery") {
    const calls = [];
    const active = {
      job_id: "recovered-job",
      state: "running",
      channels_total: 1,
      channels_completed: 0,
      messages_processed: 0,
      results: [],
    };
    stored.value = JSON.stringify({ version: 2, activeView: "sources", syncJobId: null });
    context.fetch = async (path, options) => {
      calls.push({ path, method: options.method, body: options.body ?? null });
      if (path === "/api/sync" && options.method === "GET") return response({ active, items: [active] });
      if (path.startsWith("/api/sync/")) return response(active);
      if (path === "/api/channels" || path === "/api/dialogs") return response([]);
      if (path === "/api/tags") return response({ tags: [], counts: {} });
      if (path === "/api/status") return response({ configured: false, authorized: false });
      if (path === "/api/settings" || path === "/api/ai/settings") return response({});
      if (path === "/api/search") return response({ items: [], total: 0 });
      return response({});
    };
    app.boot();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const syncCall = calls.find((entry) => entry.path === "/api/sync" && entry.method === "GET");
    assert(syncCall, "boot did not recover an active sync job from GET /api/sync");
    assert(app.state.sync.jobId === "recovered-job", "recovered sync job id was not persisted in UI state");
    return { syncCall, syncJobId: app.state.sync.jobId, calls };
  }

  if (name === "effective-tags-without-query") {
    const requests = [];
    app.state.searchMode = "natural";
    node("query").value = "";
    app.updateTagAnalysis({ interpretation: { effective_tags: ["#alpha", "#beta"] } }, true);
    assert(!node("apply-tags").disabled, "effective tags without a query did not enable the apply-tags button");
    context.fetch = async (path, options) => {
      requests.push({ path, method: options.method, body: JSON.parse(options.body) });
      return response({ items: [], total: 0, interpretation: { effective_tags: ["#alpha", "#beta"] } });
    };
    await app.runSearch({ reset: true, explicit: true });
    assert(requests.length === 1, "effective tags without a query did not issue a search");
    assert(requests[0].path === "/api/ai/search", "effective tags without a query did not use AI search");
    assert(JSON.stringify(requests[0].body.selected_tags) === JSON.stringify(["#alpha", "#beta"]),
      "effective tags without a query were not sent as selected_tags");
    assert(requests[0].body.query === "", "empty query was not preserved for tag search");
    return requests[0];
  }

  if (name === "sync-queue-delete") {
    const calls = [];
    app.state.sync.jobs = [
      { job_id: "finished-job", state: "completed", mode: "history", channels_total: 1, channels_completed: 1, messages_processed: 120 },
      { job_id: "queued-job", state: "queued", mode: "incremental", channels_total: 2, channels_completed: 0, messages_processed: 0 },
    ];
    context.fetch = async (path, options) => {
      calls.push({ path, method: options.method });
      return response({ deleted: true, job_id: "queued-job" });
    };
    app.renderSyncQueue();
    assert(node("sync-queue").innerHTML.includes('data-delete-sync-job="finished-job"'), "finished task has no independent delete control");
    assert(node("sync-queue").innerHTML.includes('data-delete-sync-job="queued-job"'), "queued task has no independent delete control");
    await app.deleteSyncTask("queued-job");
    const deletes = calls.filter((call) => call.method === "DELETE");
    assert(deletes.length === 1 && deletes[0].path.endsWith("/api/sync/queued-job"), "queue item did not delete only its own task");
    assert(app.state.sync.jobs.length === 1 && app.state.sync.jobs[0].job_id === "finished-job", "queue deletion removed another task");
    return { deletes, remaining: app.state.sync.jobs.map((job) => job.job_id) };
  }

  throw new Error("unknown frontend harness case: " + name);
}

runCase(input.case)
  .then((result) => process.stdout.write(JSON.stringify({ ok: true, result })))
  .catch((error) => {
    process.stderr.write(String(error && error.stack ? error.stack : error));
    process.exitCode = 1;
  });
"""


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = STATIC_APP.read_text(encoding="utf-8")
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def _run_frontend_case(self, name: str) -> dict[str, Any]:
        completed = subprocess.run(
            ["node", "-e", FRONTEND_NODE_HARNESS],
            cwd=ROOT,
            input=json.dumps({"source": self.source, "case": name}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout or f"frontend case {name!r} exited without output",
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload.get("ok"), payload)
        return payload.get("result", {})

    def test_frontend_uses_explicit_requests_and_strict_network_deadlines(self) -> None:
        result = self._run_frontend_case("write-no-retry")
        self.assertEqual(result["kind"], "network")
        self.assertEqual(result["requests"], [{"path": "/api/settings", "method": "POST", "body": '{"fixture":true}'}])

    def test_frontend_merges_requests_cancels_stale_work_and_prefers_latest_response(self) -> None:
        result = self._run_frontend_case("abort-latest-wins")
        self.assertTrue(result["oldAborted"])
        self.assertEqual(result["requests"], [
            {"path": "/api/search", "query": "old"},
            {"path": "/api/search", "query": "new"},
        ])
        self.assertIn('data-message-id="2"', result["results"])

    def test_frontend_uses_effective_tags_for_an_empty_natural_language_query(self) -> None:
        result = self._run_frontend_case("effective-tags-without-query")
        self.assertEqual(result["path"], "/api/ai/search")
        self.assertEqual(result["method"], "POST")
        self.assertEqual(result["body"]["query"], "")
        self.assertEqual(result["body"]["selected_tags"], ["#alpha", "#beta"])

    def test_frontend_has_one_sync_polling_path_and_recovers_offline_visibility(self) -> None:
        result = self._run_frontend_case("one-timer")
        self.assertEqual(result, {"active": 1, "clearCalls": 1, "delay": 20})

    def test_frontend_recovers_active_sync_from_get_sync_payload(self) -> None:
        result = self._run_frontend_case("sync-recovery")
        self.assertEqual(result["syncCall"], {"path": "/api/sync", "method": "GET", "body": None})
        self.assertEqual(result["syncJobId"], "recovered-job")

    def test_frontend_renders_and_deletes_sync_tasks_individually(self) -> None:
        result = self._run_frontend_case("sync-queue-delete")
        self.assertEqual(result["deletes"], [{"path": "/api/sync/queued-job", "method": "DELETE"}])
        self.assertEqual(result["remaining"], ["finished-job"])

    def test_frontend_parses_nested_relaxation_and_dictionary_count_payloads(self) -> None:
        nested = self._run_frontend_case("nested-relaxation")
        self.assertEqual(nested["initial"], 0)
        self.assertEqual(nested["final"], 2)
        self.assertEqual(
            [entry for entry in nested["trace"] if entry["removed"] == "#beta"],
            [{"label": "步骤 1", "removed": "#beta", "tags": ["#alpha"], "count": 2}],
        )
        counts = self._run_frontend_case("counts-map")
        self.assertEqual(counts, [{"tag": "#alpha", "count": 7}, {"tag": "#beta", "count": 2}])

    def test_frontend_trace_empty_state_spans_the_full_trace_grid(self) -> None:
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('<li class="trace-empty">提交自然语言搜索后显示放宽轨迹。</li>', self.source)
        self.assertIn('.trace-list .trace-empty { display: block; grid-column: 1 / -1; }', css)

    def test_frontend_renders_normalized_telegram_markdown_for_message_display(self) -> None:
        self.assertIn("<strong>#道具</strong>", self._run_frontend_case("markdown-message-text"))

    def test_frontend_has_no_removed_ai_index_reference(self) -> None:
        source = f"{self.source}\n{self.html}".lower()
        self.assertNotIn("/api/ai/index", source)
        self.assertNotIn("ai-index", source)
        self.assertNotIn("ai_index", source)

    def test_telegram_code_form_is_visible_and_uses_a_dedicated_timeout(self) -> None:
        self.assertIn('id="code-form"', self.html)
        self.assertNotIn('id="code-form" class="settings-form hidden"', self.html)
        self.assertIn("const TELEGRAM_AUTH_TIMEOUT = 45000", self.source)
        self.assertIn("const MODEL_TIMEOUT = 20000", self.source)
        self.assertIn("timeout: TELEGRAM_AUTH_TIMEOUT", self.source)
        self.assertIn("setCodeFormReady(true)", self.source)

    def test_runtime_network_log_is_documented_and_client_timeouts_are_reported(self) -> None:
        api_source = (ROOT / "telescope_core" / "api.py").read_text(encoding="utf-8")
        self.assertIn("/api/client-events", api_source)
        self.assertIn("api_request method=", api_source)
        self.assertIn("reportNetworkEvent(path, options, 'timeout'", self.source)
        self.assertIn("data/runtime/network.log", (ROOT / "README.md").read_text(encoding="utf-8"))


class DocumentationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_describes_the_new_architecture_and_explicit_ai_tag_boundary(self) -> None:
        for phrase in (
            "app.create_app",
            "local_tags",
            "GET /api/tags",
            "GET /api/sync",
            "`local`、`cloud` 和 `hybrid`",
            "不会发送给 AI",
            "不存在逐消息 AI 索引",
            "`8000` 到 `8010`",
            "TemporaryDirectory",
            "假 Telegram、假 AI 网关",
        ):
            self.assertIn(phrase, self.readme)
        self.assertNotIn("开始建立 AI 索引", self.readme)
        self.assertNotIn("为每条消息生成摘要", self.readme)


if __name__ == "__main__":
    unittest.main()

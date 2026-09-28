"""Application services coordinating persistence and injected external gateways."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import inspect
import json
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .gateways import GatewayError, RealTelegramGateway, public_gateway_error
from .models import AiModelsIn, AiSearchIn, AiSettingsIn, AiTestIn, CodeIn, SearchGroupIn, SearchIn, SettingsIn, SyncIn, SyncJob
from .observability import network_logger, release_runtime_logging
from .providers import HttpxAIGateway, ProviderError, normalize_endpoint
from .storage import TAG_RE, Storage, normalize_tag


class ApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _awaitable(value: Any) -> Awaitable[Any] | None:
    return value if inspect.isawaitable(value) else None


async def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = method(*args, **kwargs)
    pending = _awaitable(result)
    return await pending if pending is not None else result


async def _call_optional_config(method: Callable[..., Any], config: dict[str, Any], *args: Any) -> Any:
    """Call fake and real gateways with either config-aware or legacy signatures."""

    full_args = (config, *args)
    try:
        parameters = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        return await _call(method, *full_args)
    positional = [
        item for item in parameters
        if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    accepts_varargs = any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters)
    if accepts_varargs or len(positional) >= len(full_args):
        return await _call(method, *full_args)
    return await _call(method, *args)


def safe_error(error: BaseException) -> str:
    if isinstance(error, ApiError):
        allowed = {
            "The selected source is not in the last scan.",
            "Telegram gateway is not configured.",
            "One or more sources failed; see the per-source results.",
        }
        return error.detail if error.detail in allowed else "The requested operation failed."
    message = str(error)
    if isinstance(error, GatewayError):
        allowed = {
            "A saved Clash proxy is required.",
            "The saved Clash proxy port is invalid.",
            "Telegram API settings are incomplete.",
            "Telegram login is required.",
            "Telegram verification failed.",
            "Telegram gateway request failed. Check the saved connection settings.",
            "Telegram connection timed out; check the saved Clash proxy.",
        }
        return message if message in allowed else "Telegram gateway request failed. Check the saved connection settings."
    if isinstance(error, ProviderError):
        allowed = {
            "AI endpoint must be a complete http:// or https:// URL.",
            "AI endpoint cannot contain credentials, query parameters, or fragments.",
            "A cloud API key requires an HTTPS endpoint.",
            "AI provider request timed out or failed.",
            "AI provider request failed.",
            "AI provider returned invalid JSON.",
            "AI provider returned an invalid JSON object.",
            "AI provider returned invalid interpretation JSON.",
            "AI interpretation is not configured.",
            "The selected AI model was not found.",
            "No AI models were returned.",
        }
        if message in allowed or re.fullmatch(r"AI provider returned HTTP [0-9]{3}\.", message):
            return message
        return "AI provider request failed."
    return "The requested operation failed."


def _dialog_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _dialog_item(value: Any) -> dict[str, Any]:
    raw_id = _dialog_value(value, "id", _dialog_value(value, "channel_id", None))
    if raw_id is None:
        raise ApiError(502, "Telegram returned a dialog without an id.")
    return {
        "id": int(raw_id),
        "title": str(_dialog_value(value, "title", _dialog_value(value, "name", raw_id)) or raw_id),
        "username": _dialog_value(value, "username", None),
        "kind": str(_dialog_value(value, "kind", "channel") or "channel"),
    }


def _telegram_message_id(value: Any) -> int | None:
    raw_id = _dialog_value(value, "telegram_id", None)
    if raw_id is None:
        raw_id = _dialog_value(value, "message_id", _dialog_value(value, "id", None))
    try:
        return int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        return None


def _tag_rank(query: str, vocabulary: list[str]) -> list[str]:
    explicit = [normalize_tag(item) for item in TAG_RE.findall(query or "")]
    terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", TAG_RE.sub(" ", query or ""))]
    scored: list[tuple[float, str]] = []
    for position, tag in enumerate(vocabulary):
        clean = normalize_tag(tag)
        plain = clean[1:].casefold()
        score = 0.0
        if clean in explicit:
            score = 10000.0 - position
        for term in terms:
            if term == plain:
                score = max(score, 8000.0)
            elif term in plain or plain in term:
                score = max(score, 5000.0 - abs(len(term) - len(plain)) * 10)
            else:
                score = max(score, difflib.SequenceMatcher(None, term, plain).ratio() * 100.0)
        if score >= 45:
            scored.append((score, clean))
    scored.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [tag for _, tag in scored]


def _natural_relevance(query: str, item: dict[str, Any]) -> float:
    """Score a tag-selected message without turning semantic search into FTS."""

    phrase = TAG_RE.sub(" ", query or "").casefold().strip()
    text = f"{item.get('text') or ''} {item.get('album_text') or ''}".casefold()
    if not phrase or not text:
        return 0.0
    compact_phrase = re.sub(r"\s+", "", phrase)
    compact_text = re.sub(r"\s+", "", text)
    score = 0.55 if compact_phrase and compact_phrase in compact_text else 0.0
    terms = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", phrase)
    for term in terms:
        if term and term in text:
            score += min(0.25, 0.06 + len(term) * 0.025)
    # CJK messages often omit separators, so overlapping character bigrams make
    # a useful secondary signal when the complete sentence is not present.
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", phrase))
    bigrams = {cjk[index:index + 2] for index in range(max(0, len(cjk) - 1))}
    if bigrams:
        matched = sum(gram in compact_text for gram in bigrams)
        score += 0.20 * matched / len(bigrams)
    return min(1.0, score)


def _rank_natural_items(query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for item in items:
        value = dict(item)
        score = _natural_relevance(query, value)
        value["relevance_score"] = round(score, 4)
        value.setdefault("search_reasons", []).append(f"natural relevance {round(score * 100)}%")
        ranked.append(value)
    # ``Storage.search`` already orders equal-score records by newest message.
    return sorted(ranked, key=lambda item: -float(item["relevance_score"]))


_MODEL_CACHE_LIMIT = 32
_MODEL_CACHE_TTL = 300.0
_MODEL_DISCOVERY_TIMEOUT = 10.0


class TeleScopeService:
    """The single application object used by every HTTP route."""

    SYNC_WRITE_BATCH_SIZE = 50

    def __init__(self, store: Storage, telegram_gateway: Any = None, ai_gateway: Any = None):
        self.store = store
        self.telegram = telegram_gateway if telegram_gateway is not None else RealTelegramGateway(store.root)
        self.ai = ai_gateway if ai_gateway is not None else HttpxAIGateway()
        self._ai_injected = ai_gateway is not None
        self._sync_tasks: dict[str, asyncio.Task[Any]] = {}
        self._model_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
        self._model_inflight: dict[tuple[str, str, str], asyncio.Task[list[str]]] = {}
        self._model_lock = asyncio.Lock()
        self._model_generation = 0
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        if self._started:
            return
        self._stopping = False
        network_logger().info("service_start stage=storage_initialize")
        self.store.initialize()
        self._started = True
        network_logger().info("service_start stage=storage_ready")
        self.store.recover_sync_jobs()
        network_logger().info("service_start stage=sync_recovery_complete")
        self._schedule_next_sync()
        # Full-history sources are durable subscriptions. A restart schedules
        # one bounded incremental pass without re-reading their full archives.
        if not any(job.state in {"queued", "running"} for job in self.store.list_sync_jobs(limit=1000)):
            await self.create_incremental_sync(automatic=True)

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._sync_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        self._sync_tasks.clear()
        model_tasks = list(self._model_inflight.values())
        self._model_inflight.clear()
        self._model_cache.clear()
        for task in model_tasks:
            task.cancel()
        if model_tasks:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(*model_tasks, return_exceptions=True)
        for gateway in (self.telegram, self.ai):
            close = getattr(gateway, "close", None)
            if close:
                with suppress(Exception):
                    await _call(close)
        self._started = False
        release_runtime_logging(self.store.root)

    def _config(self) -> dict[str, Any]:
        value = self.store.load_config()
        nested = value.get("telegram")
        if isinstance(nested, dict):
            return {**value, **nested}
        return value

    @staticmethod
    def _public_settings(config: dict[str, Any]) -> dict[str, Any]:
        return {
            "api_id": config.get("api_id", ""),
            "phone": config.get("phone", ""),
            "proxy_type": config.get("proxy_type", "socks5"),
            "proxy_host": config.get("proxy_host", "127.0.0.1"),
            "proxy_port": config.get("proxy_port", 7890),
            "proxy_username": config.get("proxy_username", "") or "",
            "api_hash_saved": bool(config.get("api_hash")),
            "proxy_password_saved": bool(config.get("proxy_password")),
        }

    async def status(self, *, restore: bool = False) -> dict[str, Any]:
        config = self._config()
        result: dict[str, Any] = {"configured": bool(config.get("api_id") and config.get("api_hash")), "authorized": False, "session_saved": False}
        local_method = getattr(self.telegram, "local_status", None)
        if local_method:
            try:
                local = await _call_optional_config(local_method, config)
                if isinstance(local, dict):
                    result.update({
                        "authorized": bool(local.get("authorized")),
                        "configured": bool(local.get("configured", result["configured"])),
                        "session_saved": bool(local.get("session_saved")),
                    })
            except Exception:
                pass
        # A normal status read is strictly local so it never delays the page.
        # The initial UI request opts into restoration and opens the persisted
        # Telethon session automatically, without requiring a second click.
        method = getattr(self.telegram, "status", None) if restore else (getattr(self.telegram, "local_status", None) or getattr(self.telegram, "status", None))
        if method:
            try:
                raw = await asyncio.wait_for(_call_optional_config(method, config), timeout=20) if restore else await _call_optional_config(method, config)
                if isinstance(raw, dict):
                    result.update({
                        "authorized": bool(raw.get("authorized")),
                        "configured": bool(raw.get("configured", result["configured"])),
                        "session_saved": bool(raw.get("session_saved", result["session_saved"])),
                    })
                elif isinstance(raw, bool):
                    result["authorized"] = raw
            except Exception as error:
                if restore:
                    network_logger().warning("telegram_session_restore_failed type=%s", type(error).__name__)
                pass
        result["phone"] = config.get("phone", "") if result["authorized"] else config.get("phone", "")
        return result

    async def get_settings(self) -> dict[str, Any]:
        return self._public_settings(self._config())

    async def save_settings(self, payload: SettingsIn) -> dict[str, bool]:
        config = self.store.load_config()
        values = payload.model_dump()
        current = self._config()
        for key in ("api_hash", "proxy_password"):
            if not values.get(key) and current.get(key):
                values[key] = current[key]
        nested = config.get("telegram")
        if isinstance(nested, dict):
            nested.update(values)
        else:
            config.update(values)
        self.store.save_config(config)
        reset = getattr(self.telegram, "reset", None)
        if reset:
            with suppress(Exception):
                await _call(reset)
        return {"saved": True}

    async def send_code(self) -> dict[str, bool]:
        method = getattr(self.telegram, "send_code", None) or getattr(self.telegram, "request_code", None)
        if not method:
            raise ApiError(503, "Telegram gateway is not configured.")
        try:
            raw = await _call_optional_config(method, self._config())
        except Exception as error:
            raise ApiError(503, safe_error(error)) from error
        if isinstance(raw, dict):
            return {"sent": bool(raw.get("sent", not raw.get("authorized", False))), "authorized": bool(raw.get("authorized"))}
        return {"sent": bool(raw), "authorized": False}

    async def verify_code(self, payload: CodeIn) -> dict[str, bool]:
        method = getattr(self.telegram, "verify", None) or getattr(self.telegram, "sign_in", None)
        if not method:
            raise ApiError(503, "Telegram gateway is not configured.")
        try:
            raw = await _call_optional_config(method, self._config(), payload.code, payload.password)
        except Exception as error:
            raise ApiError(400, safe_error(error)) from error
        return {"authorized": bool(raw.get("authorized", True)) if isinstance(raw, dict) else True}

    async def scan_channels(self) -> dict[str, Any]:
        method = getattr(self.telegram, "scan_dialogs", None) or getattr(self.telegram, "scan", None)
        if not method:
            raise ApiError(503, "Telegram gateway is not configured.")
        try:
            raw = await _call_optional_config(method, self._config())
            items = [_dialog_item(item) for item in (raw or [])]
            saved = self.store.save_scanned_dialogs(items)
            return {"items": saved, "count": len(saved)}
        except ApiError:
            raise
        except Exception as error:
            raise ApiError(503, safe_error(error)) from error

    async def dialogs(self) -> list[dict[str, Any]]:
        return self.store.scanned_dialogs()

    async def channels(self) -> list[dict[str, Any]]:
        return self.store.channels()

    def _search_groups(self) -> list[dict[str, Any]]:
        raw = self.store.load_config().get("search_groups", [])
        if not isinstance(raw, list):
            return []
        available = {int(channel["id"]) for channel in self.store.channels()}
        groups: list[dict[str, Any]] = []
        for value in raw:
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or "").strip()[:48]
            group_id = str(value.get("id") or "").strip()
            channel_ids = list(dict.fromkeys(int(item) for item in value.get("channel_ids", []) if str(item).lstrip("-").isdigit() and int(item) in available))
            if group_id and name and channel_ids:
                groups.append({"id": group_id, "name": name, "channel_ids": channel_ids})
        return groups

    async def search_groups(self) -> list[dict[str, Any]]:
        return self._search_groups()

    async def create_search_group(self, payload: SearchGroupIn) -> dict[str, Any]:
        name = payload.name.strip()
        available = {int(channel["id"]) for channel in self.store.channels()}
        channel_ids = list(dict.fromkeys(payload.channel_ids))
        if not name or any(channel_id not in available for channel_id in channel_ids):
            raise ApiError(400, "A search group must contain indexed channels.")
        groups = self._search_groups()
        if any(group["name"].casefold() == name.casefold() for group in groups):
            raise ApiError(409, "A search group with this name already exists.")
        group = {"id": uuid4().hex, "name": name, "channel_ids": channel_ids}
        config = self.store.load_config()
        config["search_groups"] = [*groups, group]
        self.store.save_config(config)
        return group

    async def update_search_group(self, group_id: str, payload: SearchGroupIn) -> dict[str, Any]:
        name = payload.name.strip()
        available = {int(channel["id"]) for channel in self.store.channels()}
        channel_ids = list(dict.fromkeys(payload.channel_ids))
        if not name or any(channel_id not in available for channel_id in channel_ids):
            raise ApiError(400, "A search group must contain indexed channels.")
        groups = self._search_groups()
        current = next((group for group in groups if group["id"] == group_id), None)
        if not current:
            raise ApiError(404, "Search group not found.")
        if any(group["id"] != group_id and group["name"].casefold() == name.casefold() for group in groups):
            raise ApiError(409, "A search group with this name already exists.")
        updated = {"id": group_id, "name": name, "channel_ids": channel_ids}
        config = self.store.load_config()
        config["search_groups"] = [updated if group["id"] == group_id else group for group in groups]
        self.store.save_config(config)
        return updated

    async def delete_search_group(self, group_id: str) -> dict[str, bool]:
        groups = self._search_groups()
        retained = [group for group in groups if group["id"] != group_id]
        if len(retained) == len(groups):
            raise ApiError(404, "Search group not found.")
        config = self.store.load_config()
        config["search_groups"] = retained
        self.store.save_config(config)
        return {"deleted": True}

    def _fingerprint(self, payload: SyncIn) -> str:
        raw = json.dumps([sorted(set(payload.channel_ids)), payload.limit_per_channel, payload.mode], separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def create_sync(self, payload: SyncIn) -> dict[str, Any]:
        requested = list(dict.fromkeys(payload.channel_ids))
        dialogs = self.store.scanned_dialogs(requested) if payload.mode == "full" else self.store.indexed_channels(requested)
        if len(dialogs) != len(requested):
            raise ApiError(400, "The selected source is not available for this sync.")
        fingerprint = self._fingerprint(payload)
        existing = next(
            (
                job for job in self.store.list_sync_jobs(limit=1000)
                if job.fingerprint == fingerprint and job.state in {"queued", "running"}
            ),
            None,
        )
        if existing:
            self._schedule_next_sync()
            return existing.snapshot()
        job = SyncJob(
            job_id=uuid4().hex, channel_ids=requested, limit_per_channel=payload.limit_per_channel, mode=payload.mode,
            channels_total=len(dialogs), fingerprint=fingerprint,
        )
        self.store.save_sync_job(job)
        self._schedule_next_sync()
        return job.snapshot()

    async def create_incremental_sync(self, *, automatic: bool = False) -> dict[str, Any] | None:
        channel_ids = self.store.auto_update_channel_ids()
        if not channel_ids:
            return None
        # Telegram returns newest messages first; this cap only protects a long
        # offline period while ``min_id`` ensures old history is never fetched.
        payload = SyncIn(channel_ids=channel_ids, limit_per_channel=10000, mode="incremental")
        result = await self.create_sync(payload)
        network_logger().info(
            "incremental_sync_scheduled automatic=%s channel_count=%s job_id=%s",
            automatic, len(channel_ids), result.get("job_id"),
        )
        return result

    async def create_verification_sync(self, channel_id: int) -> dict[str, Any]:
        """Re-read one local source to repair missing or changed remote records."""

        if channel_id not in {int(channel["id"]) for channel in self.store.channels()}:
            raise ApiError(404, "Indexed source not found.")
        return await self.create_sync(SyncIn(channel_ids=[channel_id], limit_per_channel=None, mode="verify"))

    async def sync_jobs(self) -> dict[str, Any]:
        jobs = [job.snapshot() for job in self.store.list_sync_jobs()]
        active = next((job for job in jobs if job["state"] == "running"), None)
        if active is None:
            active = next((job for job in reversed(jobs) if job["state"] == "queued"), None)
        return {"items": jobs, "jobs": jobs, "active": active}

    async def sync_status(self, job_id: str) -> dict[str, Any]:
        job = self.store.load_sync_job(job_id)
        if not job:
            raise ApiError(404, "Sync job not found.")
        return job.snapshot()

    async def delete_sync_job(self, job_id: str) -> dict[str, Any]:
        """Stop a task before removing its durable status record."""

        task = self._sync_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._sync_tasks.pop(job_id, None)
        if not self.store.delete_sync_job(job_id):
            raise ApiError(404, "Sync job not found.")
        network_logger().info("sync_job_deleted job_id=%s", job_id)
        self._schedule_next_sync()
        return {"job_id": job_id, "deleted": True}

    def _schedule_next_sync(self) -> None:
        """Run exactly one queued sync at a time, in durable FIFO order."""

        if self._stopping:
            return
        if any(not task.done() for task in self._sync_tasks.values()):
            return
        job = self.store.next_queued_sync_job()
        if not job:
            return
        job_id = job.job_id
        task = asyncio.create_task(self._run_sync(job_id))
        self._sync_tasks[job_id] = task

        def finished(_: asyncio.Task[Any]) -> None:
            self._sync_tasks.pop(job_id, None)
            if not self._stopping:
                self._schedule_next_sync()

        task.add_done_callback(finished)

    def _job_result(self, job: SyncJob, dialog: dict[str, Any]) -> dict[str, Any]:
        for result in job.results:
            if int(result.get("channel_id", -1)) == int(dialog["id"]):
                return result
        result = {"channel_id": dialog["id"], "title": dialog["title"], "state": "running", "processed": 0, "stored_total": 0, "error": None}
        job.results.append(result)
        return result

    async def _store_messages(self, job: SyncJob, dialog: dict[str, Any], messages: list[Any]) -> None:
        """Commit bounded batches so SQLite/FTS work cannot starve the API loop."""

        if not messages:
            return
        result = self._job_result(job, dialog)
        stored = self.store.upsert_messages(dialog, messages, refresh_tags=False)
        observed = int(stored["observed"])
        result["processed"] = int(result.get("processed", 0)) + observed
        result["stored_total"] = stored["stored_total"]
        job.messages_processed += observed
        job.current = {"channel_id": dialog["id"], "title": dialog["title"], "processed": result["processed"], "target": job.limit_per_channel}
        self.store.save_sync_job(job)
        await asyncio.sleep(0)

    async def _store_message(self, job: SyncJob, dialog: dict[str, Any], message: Any) -> None:
        await self._store_messages(job, dialog, [message])

    async def _iterate_gateway(
        self, method: Any, dialog: dict[str, Any], limit: int | None, job: SyncJob,
        remote_ids: set[int] | None = None, *, minimum_id: int = 0, maximum_id: int = 0,
    ) -> int:
        try:
            stream = method(dialog, limit, min_id=minimum_id, max_id=maximum_id)
        except TypeError:
            try:
                stream = method(dialog, limit, min_id=minimum_id)
            except TypeError:
                # Compatibility for injected gateways predating cursor support.
                stream = method(dialog, limit)
        if inspect.isawaitable(stream):
            stream = await stream
        batch: list[Any] = []
        received = 0

        async def flush() -> None:
            nonlocal batch
            await self._store_messages(job, dialog, batch)
            batch = []

        if hasattr(stream, "__aiter__"):
            async for message in stream:
                received += 1
                message_id = _telegram_message_id(message)
                if remote_ids is not None and message_id is not None:
                    remote_ids.add(message_id)
                batch.append(message)
                if len(batch) >= self.SYNC_WRITE_BATCH_SIZE:
                    await flush()
        else:
            for message in stream or []:
                received += 1
                message_id = _telegram_message_id(message)
                if remote_ids is not None and message_id is not None:
                    remote_ids.add(message_id)
                batch.append(message)
                if len(batch) >= self.SYNC_WRITE_BATCH_SIZE:
                    await flush()
        await flush()
        return received

    async def _sync_archive_windows(
        self, method: Any, dialog: dict[str, Any], job: SyncJob, remote_ids: set[int] | None,
    ) -> str:
        """Read only remote windows not already covered by this local archive."""

        bounds = self.store.sync_bounds(int(dialog["id"]))
        if job.mode == "verify":
            await self._iterate_gateway(method, dialog, None, job, remote_ids)
            return "remote-verify"
        if job.mode == "incremental" or not bounds:
            minimum_id = int(bounds["last_id"]) if bounds else 0
            await self._iterate_gateway(method, dialog, job.limit_per_channel, job, remote_ids, minimum_id=minimum_id)
            return "incremental" if bounds else "initial"

        limit = job.limit_per_channel
        received_new = await self._iterate_gateway(
            method, dialog, limit, job, remote_ids, minimum_id=int(bounds["last_id"]),
        )
        if bool(bounds["complete"]):
            return "incremental"
        remaining = None if limit is None else max(0, limit - received_new)
        if remaining is None or remaining > 0:
            await self._iterate_gateway(
                method, dialog, remaining, job, remote_ids, maximum_id=int(bounds["first_id"]),
            )
            return "new-and-backfill"
        return "incremental"

    async def _run_gateway_sync(self, dialogs: list[dict[str, Any]], job: SyncJob) -> None:
        method = getattr(self.telegram, "sync", None)
        if not method:
            raise ApiError(503, "Telegram gateway is not configured.")

        async def on_message(dialog: dict[str, Any], message: Any = None) -> None:
            if message is None:
                message = dialog
                dialog = dialogs[0]
            await self._store_message(job, _dialog_item(dialog), message)

        params = list(inspect.signature(method).parameters.values())
        third_name = params[2].name.lower() if len(params) >= 3 else ""
        try:
            if third_name in {"job", "state", "progress"}:
                result = await _call(method, dialogs, job.limit_per_channel, job)
            elif len(params) >= 4:
                result = await _call(method, dialogs, job.limit_per_channel, on_message, lambda: False)
            elif len(params) == 3:
                result = await _call(method, dialogs, job.limit_per_channel, on_message)
            else:
                result = await _call(method, dialogs, job.limit_per_channel)
        except TypeError:
            result = await _call(method, dialogs, job.limit_per_channel, job)
        if isinstance(result, (list, tuple)):
            for item in result:
                if isinstance(item, dict) and "message" in item:
                    await self._store_message(job, _dialog_item(item.get("dialog", dialogs[0])), item["message"])
        for dialog in dialogs:
            result_item = self._job_result(job, dialog)
            if result_item["state"] not in {"failed", "completed"}:
                result_item["state"] = "completed"
                result_item["stored_total"] = self.store.channels_for_id(dialog["id"])
        job.channels_completed = sum(1 for item in job.results if item.get("state") == "completed")
        job.current = None
        self.store.save_sync_job(job)

    async def _run_sync(self, job_id: str) -> None:
        job = self.store.load_sync_job(job_id)
        if not job:
            return
        try:
            dialogs = self.store.scanned_dialogs(job.channel_ids) if job.mode == "full" else self.store.indexed_channels(job.channel_ids)
            if len(dialogs) != len(set(job.channel_ids)):
                raise ApiError(400, "The selected source is not in the last scan.")
            job.channels_total = len(dialogs)
            job.state = "running"
            job.error = None
            self.store.save_sync_job(job)
            pending = [dialog for dialog in dialogs if self._job_result(job, dialog).get("state") != "completed"]
            iter_method = getattr(self.telegram, "iter_messages", None)
            if iter_method:
                try:
                    self.telegram._active_config = self._config()
                except (AttributeError, TypeError):
                    pass
                for dialog in pending:
                    result = self._job_result(job, dialog)
                    local_ids_before = self.store.telegram_message_ids(int(dialog["id"])) if job.mode == "verify" else None
                    remote_ids = set() if job.mode == "verify" else None
                    result.update({"title": dialog["title"], "state": "running", "error": None})
                    job.current = {"channel_id": dialog["id"], "title": dialog["title"], "processed": result.get("processed", 0), "target": job.limit_per_channel}
                    self.store.save_sync_job(job)
                    try:
                        result["strategy"] = await self._sync_archive_windows(iter_method, dialog, job, remote_ids)
                        if local_ids_before is not None and remote_ids is not None:
                            result["verification"] = {
                                "verified": True,
                                "local_before_count": len(local_ids_before),
                                "remote_visible_count": len(remote_ids),
                                "restored_count": len(remote_ids - local_ids_before),
                                "local_only_count": len(local_ids_before - remote_ids),
                            }
                        result["state"] = "completed"
                        self.store.mark_channel_sync_completed(
                            dialog["id"], completed_full_history=job.mode in {"full", "verify"} and job.limit_per_channel is None
                        )
                        job.channels_completed = sum(1 for item in job.results if item.get("state") == "completed")
                        self.store.save_sync_job(job)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        result["state"] = "failed"
                        result["error"] = safe_error(error)
                        job.channels_completed = sum(1 for item in job.results if item.get("state") == "completed")
                        self.store.save_sync_job(job)
                    finally:
                        self.store.refresh_tag_dictionary()
                        result["integrity"] = self.store.verify_message_id_order(int(dialog["id"]))
                        self.store.save_sync_job(job)
            else:
                await self._run_gateway_sync(pending, job)
            job.state = "completed" if all(item.get("state") == "completed" for item in job.results) else "failed"
            if job.state == "failed":
                job.error = "One or more sources failed; see the per-source results."
            job.current = None
            self.store.save_sync_job(job)
        except asyncio.CancelledError:
            job.state = "queued"
            job.current = None
            self.store.save_sync_job(job)
            raise
        except Exception as error:
            job.state = "failed"
            job.error = safe_error(error)
            job.current = None
            self.store.save_sync_job(job)

    async def delete_channel(self, channel_id: int) -> dict[str, Any]:
        for job_id, task in self._sync_tasks.items():
            if not task.done():
                job = self.store.load_sync_job(job_id)
                if job and channel_id in job.channel_ids:
                    raise ApiError(409, "The source cannot be deleted while it is syncing.")
        try:
            result = self.store.delete_channel(channel_id)
            config = self.store.load_config()
            raw_groups = config.get("search_groups", [])
            if isinstance(raw_groups, list):
                groups: list[dict[str, Any]] = []
                for group in raw_groups:
                    if not isinstance(group, dict):
                        continue
                    remaining = [int(item) for item in group.get("channel_ids", []) if str(item).lstrip("-").isdigit() and int(item) != channel_id]
                    if remaining:
                        groups.append({**group, "channel_ids": list(dict.fromkeys(remaining))})
                if groups != raw_groups:
                    config["search_groups"] = groups
                    self.store.save_config(config)
            return result
        except KeyError as error:
            raise ApiError(404, "Indexed source not found.") from error

    async def search(self, payload: SearchIn) -> dict[str, Any]:
        return self.store.search(payload, ad_keywords=self.ad_filter_settings()["keywords"])

    def ad_filter_settings(self) -> dict[str, Any]:
        raw = self.store.load_config().get("ad_filters", {})
        raw = raw if isinstance(raw, dict) else {}
        keywords = list(dict.fromkeys(str(item).strip()[:80] for item in raw.get("keywords", []) if str(item).strip()))[:100]
        return {"keywords": keywords}

    async def save_ad_filter_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_keywords = payload.get("keywords", []) if isinstance(payload, dict) else []
        if not isinstance(raw_keywords, list):
            raise ApiError(400, "Advertisement keywords must be a list.")
        keywords = list(dict.fromkeys(str(item).strip()[:80] for item in raw_keywords if str(item).strip()))[:100]
        config = self.store.load_config()
        config["ad_filters"] = {"keywords": keywords}
        self.store.save_config(config)
        return {"keywords": keywords}

    def _ai_config(self) -> dict[str, Any]:
        defaults = AiSettingsIn().model_dump()
        saved = self.store.load_config().get("ai", {})
        return {**defaults, **saved} if isinstance(saved, dict) else defaults

    async def get_ai_settings(self) -> dict[str, Any]:
        config = self._ai_config()
        return {**config, "cloud_api_key": bool(config.get("cloud_api_key"))}

    async def save_ai_settings(self, payload: AiSettingsIn) -> dict[str, Any]:
        config = self.store.load_config()
        current = config.get("ai", {}) if isinstance(config.get("ai", {}), dict) else {}
        values = payload.model_dump()
        if not values.get("cloud_api_key") and current.get("cloud_api_key"):
            values["cloud_api_key"] = current["cloud_api_key"]
        config["ai"] = values
        self.store.save_config(config)
        await self._invalidate_model_discovery()
        return await self.get_ai_settings()

    async def _invalidate_model_discovery(self) -> None:
        async with self._model_lock:
            tasks = list(self._model_inflight.values())
            self._model_inflight.clear()
            self._model_cache.clear()
            self._model_generation += 1
        for task in tasks:
            task.cancel()

    def _provider_candidates(self, config: dict[str, Any]) -> list[tuple[str, str, str, str]]:
        mode = config.get("mode", "disabled")
        local = ("local", str(config.get("local_base_url") or ""), str(config.get("local_model") or ""), "")
        cloud = ("cloud", str(config.get("cloud_base_url") or ""), str(config.get("cloud_model") or ""), str(config.get("cloud_api_key") or ""))
        candidates: list[tuple[str, str, str, str]] = []
        if mode in {"local", "hybrid"} and local[1] and local[2]:
            candidates.append(local)
        if mode in {"cloud", "hybrid"} and cloud[1] and cloud[2]:
            candidates.append(cloud)
        return candidates

    def _validate_ai_tags(self, raw: Any, vocabulary: list[str]) -> list[str]:
        allowed = {normalize_tag(tag).casefold(): normalize_tag(tag) for tag in vocabulary}
        if isinstance(raw, dict):
            raw = raw.get("ranked_tags", raw.get("selected_tags", raw.get("tags", [])))
        if not isinstance(raw, list):
            return []
        values: list[str] = []
        for item in raw:
            value = item.get("tag") if isinstance(item, dict) else item
            normalized = allowed.get(normalize_tag(str(value)).casefold())
            if normalized and normalized not in values:
                values.append(normalized)
        return values

    async def _call_interpreter(self, candidate: tuple[str, str, str, str], query: str, vocabulary: list[str], config: dict[str, Any]) -> dict[str, Any]:
        method = getattr(self.ai, "interpret", None) or getattr(self.ai, "interpret_query", None) or getattr(self.ai, "search", None)
        if not method:
            raise ProviderError("AI interpretation is not configured.")
        provider, base, model, api_key = candidate
        values = {
            "provider": provider,
            "base_url": base,
            "model": model,
            "api_key": api_key,
            "query": query,
            "vocabulary": vocabulary,
            "tags": vocabulary,
            "config": config,
        }
        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            parameters = []
        positional = [
            item for item in parameters
            if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        required = [
            item for item in parameters
            if item.default is inspect.Parameter.empty
            and item.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        ]
        has_varargs = any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters)
        has_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
        has_positional_only = any(item.kind == inspect.Parameter.POSITIONAL_ONLY for item in parameters)
        if has_kwargs and not has_positional_only:
            raw = await _call(method, **values)
        elif parameters and not has_varargs and all(item.name in values for item in required):
            args: list[Any] = []
            kwargs: dict[str, Any] = {}
            for item in parameters:
                if item.name not in values:
                    continue
                if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
                    args.append(values[item.name])
                elif item.kind == inspect.Parameter.KEYWORD_ONLY:
                    kwargs[item.name] = values[item.name]
            raw = await _call(method, *args, **kwargs)
        elif len(positional) >= 6 or has_varargs:
            raw = await _call(method, provider, base, model, api_key, query, vocabulary)
        elif len(positional) >= 3:
            raw = await _call(method, query, vocabulary, config)
        else:
            raw = await _call(method, query, vocabulary)
        return raw if isinstance(raw, dict) else {"ranked_tags": raw, "provider": provider}

    async def _interpret(self, payload: AiSearchIn, vocabulary: list[str]) -> tuple[list[str], str]:
        config = self._ai_config()
        deterministic = _tag_rank(payload.query, vocabulary)
        candidates = self._provider_candidates(config)
        if not self._ai_injected and not candidates:
            return deterministic, "dictionary"
        if self._ai_injected and not candidates:
            candidates = [("injected", "", "", "")]
        failures = 0
        for candidate in candidates[:2]:
            try:
                raw = await self._call_interpreter(candidate, payload.query, vocabulary, config)
                tags = self._validate_ai_tags(raw, vocabulary)
                return tags or deterministic, candidate[0]
            except Exception:
                failures += 1
                if failures >= 2:
                    break
        return deterministic, "dictionary"

    async def ai_search(self, payload: AiSearchIn) -> dict[str, Any]:
        dictionary = self.store.tag_dictionary()
        vocabulary = dictionary["tags"]
        requested_tags = self._validate_ai_tags(payload.selected_tags, vocabulary)
        should_interpret = bool(payload.use_ai and payload.query.strip() and not requested_tags)
        if should_interpret:
            ranked_tags, provider = await self._interpret(payload, vocabulary)
        else:
            ranked_tags = requested_tags
            provider = "dictionary"
        search_data = payload.model_dump()
        # The natural-language phrase becomes a second-stage relevance score,
        # not an FTS WHERE condition. This keeps valid tag candidates visible.
        search_data.update({"query": "", "selected_tags": ranked_tags})
        search_payload = SearchIn.model_validate(search_data)
        initial = self.store.search(search_payload, ad_keywords=self.ad_filter_settings()["keywords"])
        effective = list(ranked_tags)
        steps: list[dict[str, Any]] = []
        current = initial
        while effective and current["total"] < payload.minimum_results:
            removed = effective.pop()
            search_data["selected_tags"] = effective
            current = self.store.search(SearchIn.model_validate(search_data), ad_keywords=self.ad_filter_settings()["keywords"])
            steps.append({
                "removed_tag": removed, "tag": removed, "remaining_tags": list(effective),
                "count": current["total"], "total": current["total"],
            })
        interpretation = {
            "query": payload.query,
            "dictionary_version": dictionary["dictionary_version"],
            "ranked_tags": ranked_tags,
            "matched_tags": ranked_tags,
            "effective_tags": effective,
            "provider": provider,
            "editable": True,
        }
        relaxation = {
            "threshold": payload.minimum_results,
            "initial_count": initial["total"],
            "final_count": current["total"],
            "steps": steps,
        }
        return {
            "items": _rank_natural_items(payload.query, current["items"]),
            "total": current["total"],
            "interpretation": interpretation,
            "relaxation": relaxation,
        }

    async def list_ai_models(self, payload: AiModelsIn) -> dict[str, Any]:
        config = self._ai_config()
        api_key = payload.api_key
        if not api_key and payload.provider == "cloud":
            saved_url = str(config.get("cloud_base_url") or "")
            try:
                if normalize_endpoint("cloud", saved_url, str(config.get("cloud_api_key") or "")) == normalize_endpoint("cloud", payload.base_url, str(config.get("cloud_api_key") or "")):
                    api_key = str(config.get("cloud_api_key") or "")
            except ProviderError:
                pass
        method = getattr(self.ai, "models", None) or getattr(self.ai, "available_models", None)
        if not method:
            raise ApiError(503, "AI provider is not configured.")
        try:
            endpoint = normalize_endpoint(payload.provider, payload.base_url, api_key)
        except ProviderError as error:
            raise ApiError(error.status_code, safe_error(error)) from error
        credential_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        cache_key = (payload.provider, endpoint, credential_hash)

        async def discover() -> list[str]:
            values = await _call(method, payload.provider, payload.base_url, api_key)
            return [str(value) for value in (values or [])]

        try:
            now = asyncio.get_running_loop().time()
            async with self._model_lock:
                cached = self._model_cache.get(cache_key)
                if cached and cached[0] > now:
                    return {"models": list(cached[1])}
                if cached:
                    self._model_cache.pop(cache_key, None)
                task = self._model_inflight.get(cache_key)
                if task is None:
                    task = asyncio.create_task(discover())
                    self._model_inflight[cache_key] = task

                    def remove_finished(done: asyncio.Task[list[str]]) -> None:
                        if self._model_inflight.get(cache_key) is done:
                            self._model_inflight.pop(cache_key, None)

                    task.add_done_callback(remove_finished)
                generation = self._model_generation
            async with asyncio.timeout(_MODEL_DISCOVERY_TIMEOUT):
                models = await asyncio.shield(task)
            async with self._model_lock:
                if generation != self._model_generation:
                    return {"models": list(models)}
                now = asyncio.get_running_loop().time()
                for key, (expires_at, _) in list(self._model_cache.items()):
                    if expires_at <= now:
                        self._model_cache.pop(key, None)
                self._model_cache[cache_key] = (now + _MODEL_CACHE_TTL, list(models))
                while len(self._model_cache) > _MODEL_CACHE_LIMIT:
                    oldest = min(self._model_cache, key=lambda key: self._model_cache[key][0])
                    self._model_cache.pop(oldest, None)
            return {"models": list(models)}
        except TimeoutError as error:
            raise ApiError(504, "AI provider request timed out or failed.") from error
        except ProviderError as error:
            raise ApiError(error.status_code, safe_error(error)) from error
        except Exception as error:
            raise ApiError(502, safe_error(error)) from error

    async def test_ai(self, payload: AiTestIn | None = None) -> dict[str, str]:
        config = self._ai_config()
        candidates = self._provider_candidates(config)
        if payload is not None:
            api_key = payload.api_key
            if not api_key and payload.provider == "cloud":
                saved_base = str(config.get("cloud_base_url") or "")
                saved_key = str(config.get("cloud_api_key") or "")
                try:
                    if normalize_endpoint("cloud", saved_base, saved_key) == normalize_endpoint("cloud", payload.base_url, saved_key):
                        api_key = saved_key
                except ProviderError:
                    pass
            try:
                endpoint = normalize_endpoint(payload.provider, payload.base_url, api_key)
            except ProviderError as error:
                raise ApiError(error.status_code, safe_error(error)) from error
            candidates = [(payload.provider, endpoint, payload.model, api_key)]
        if not candidates:
            raise ApiError(400, "Save an enabled AI provider and model first.")
        method = getattr(self.ai, "test", None) or getattr(self.ai, "test_connection", None)
        if not method:
            raise ApiError(503, "AI provider is not configured.")
        last_error: BaseException | None = None
        for candidate in candidates[:2]:
            try:
                result = await _call(method, candidate[0], candidate[1], candidate[2], candidate[3])
                value = result if isinstance(result, dict) else {"provider": candidate[0], "model": candidate[2]}
                return {"message": "AI connection succeeded", "provider": str(value.get("provider", candidate[0])), "model": str(value.get("model", candidate[2]))}
            except Exception as error:
                last_error = error
        raise ApiError(getattr(last_error, "status_code", 502), safe_error(last_error or RuntimeError()))

    async def bookmark(self, message_id: int, bookmarked: bool) -> dict[str, bool]:
        try:
            return {"bookmarked": self.store.set_bookmark(message_id, bookmarked)}
        except KeyError as error:
            raise ApiError(404, "Message not found.") from error

    async def image_preview(self, message_id: int) -> tuple[bytes, str]:
        try:
            locator = self.store.message_media(message_id)
        except KeyError as error:
            raise ApiError(404, "Message not found.") from error
        if locator["media_type"] != "image":
            raise ApiError(404, "This message has no image preview.")
        method = getattr(self.telegram, "preview_image", None)
        if not method:
            raise ApiError(503, "Telegram image previews are not configured.")
        try:
            raw = await _call_optional_config(method, self._config(), locator["channel_id"], locator["telegram_id"])
        except Exception as error:
            raise ApiError(503, safe_error(error)) from error
        if not isinstance(raw, tuple) or len(raw) != 2 or not isinstance(raw[0], bytes) or not raw[0]:
            raise ApiError(502, "Telegram image preview could not be read.")
        return raw

    async def video_thumbnail(self, message_id: int) -> tuple[bytes, str]:
        try:
            locator = self.store.message_media(message_id)
        except KeyError as error:
            raise ApiError(404, "Message not found.") from error
        if locator["media_type"] != "video":
            raise ApiError(404, "This message has no video thumbnail.")
        method = getattr(self.telegram, "video_thumbnail", None)
        if not method:
            raise ApiError(503, "Telegram video thumbnails are not configured.")
        try:
            raw = await _call_optional_config(method, self._config(), locator["channel_id"], locator["telegram_id"])
        except Exception as error:
            raise ApiError(503, safe_error(error)) from error
        if not isinstance(raw, tuple) or len(raw) != 2 or not isinstance(raw[0], bytes) or not raw[0]:
            raise ApiError(502, "Telegram video thumbnail could not be read.")
        return raw

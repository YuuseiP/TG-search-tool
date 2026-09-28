"""Bounded, mergeable HTTPX transport for explicit AI search operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .observability import network_logger


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def normalize_endpoint(provider: str, value: str, api_key: str = "") -> str:
    raw = (value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderError("AI endpoint must be a complete http:// or https:// URL.", status_code=400)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderError("AI endpoint cannot contain credentials, query parameters, or fragments.", status_code=400)
    if provider == "cloud" and api_key and parsed.scheme != "https":
        raise ProviderError("A cloud API key requires an HTTPS endpoint.", status_code=400)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: dict[str, Any]


class HttpxAIGateway:
    """One connection pool with cache and in-flight request coalescing.

    GET requests may retry once because model discovery is safe to repeat. POST
    requests are sent once; cancellation is propagated to the caller while a
    shared in-flight request remains bounded and is closed with the gateway.
    """

    def __init__(
        self,
        *,
        connect_timeout: float = 2.0,
        read_timeout: float = 8.0,
        overall_timeout: float = 12.0,
        cache_ttl: float = 300.0,
    ):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.overall_timeout = overall_timeout
        self.cache_ttl = cache_ttl
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._cache: dict[str, _CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}

    async def _client_for(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.read_timeout, connect=self.connect_timeout),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=30),
                follow_redirects=False,
            )
        return self._client

    @staticmethod
    def _request_key(method: str, url: str, payload: dict[str, Any] | None, api_key: str) -> str:
        secret_marker = hashlib.sha256(api_key.encode("utf-8")).hexdigest() if api_key else ""
        raw = json.dumps([method, url, payload or {}, secret_marker], sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _request_once(
        self, method: str, url: str, payload: dict[str, Any] | None, api_key: str,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        client = await self._client_for()
        endpoint = urlsplit(url)
        safe_endpoint = f"{endpoint.scheme}://{endpoint.netloc}{endpoint.path}"
        started = asyncio.get_running_loop().time()
        network_logger().info("ai_http_start method=%s endpoint=%s", method, safe_endpoint)
        deadline = asyncio.get_running_loop().time() + self.overall_timeout
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderError("AI provider request timed out or failed.", status_code=504)
            try:
                async with asyncio.timeout(remaining):
                    response = await client.request(method, url, json=payload, headers=headers)
                if response.status_code >= 400:
                    network_logger().warning("ai_http_response method=%s endpoint=%s status=%s duration_ms=%d", method, safe_endpoint, response.status_code, (asyncio.get_running_loop().time() - started) * 1000)
                    raise ProviderError(f"AI provider returned HTTP {response.status_code}.", status_code=502)
                value = response.json()
                if not isinstance(value, dict):
                    raise ProviderError("AI provider returned an invalid JSON object.")
                network_logger().info("ai_http_response method=%s endpoint=%s status=%s duration_ms=%d", method, safe_endpoint, response.status_code, (asyncio.get_running_loop().time() - started) * 1000)
                return value
            except ProviderError:
                raise
            except (asyncio.TimeoutError, httpx.TimeoutException, httpx.TransportError) as error:
                if attempt == 0 and method == "GET":
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        network_logger().warning("ai_http_failed method=%s endpoint=%s kind=%s duration_ms=%d", method, safe_endpoint, type(error).__name__, (asyncio.get_running_loop().time() - started) * 1000)
                        raise ProviderError("AI provider request timed out or failed.", status_code=504) from error
                    await asyncio.sleep(min(0.05, remaining))
                    continue
                network_logger().warning("ai_http_failed method=%s endpoint=%s kind=%s duration_ms=%d", method, safe_endpoint, type(error).__name__, (asyncio.get_running_loop().time() - started) * 1000)
                raise ProviderError("AI provider request timed out or failed.", status_code=504) from error
            except (ValueError, json.JSONDecodeError) as error:
                network_logger().warning("ai_http_failed method=%s endpoint=%s kind=%s duration_ms=%d", method, safe_endpoint, type(error).__name__, (asyncio.get_running_loop().time() - started) * 1000)
                raise ProviderError("AI provider returned invalid JSON.") from error
        raise ProviderError("AI provider request failed.")

    async def request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        api_key: str = "",
        cache: bool = True,
    ) -> dict[str, Any]:
        method = method.upper()
        key = self._request_key(method, url, payload, api_key)
        now = asyncio.get_running_loop().time()
        async with self._lock:
            cached = self._cache.get(key)
            if cache and cached and cached.expires_at > now:
                return cached.value
            if cached and cached.expires_at <= now:
                self._cache.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._request_once(method, url, payload, api_key))
                self._inflight[key] = task

                def remove_finished(done: asyncio.Task[dict[str, Any]]) -> None:
                    if self._inflight.get(key) is done:
                        self._inflight.pop(key, None)

                task.add_done_callback(remove_finished)
        value = await asyncio.shield(task)
        if cache:
            async with self._lock:
                self._cache[key] = _CacheEntry(asyncio.get_running_loop().time() + self.cache_ttl, value)
        return value

    async def models(self, provider: str, base_url: str, api_key: str = "") -> list[str]:
        base = normalize_endpoint(provider, base_url, api_key)
        url = f"{base}/api/tags" if provider == "local" else f"{base}/models"
        response = await self.request_json("GET", url, api_key=api_key)
        raw = response.get("models", []) if provider == "local" else response.get("data", [])
        models: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            value = item.get("name") if isinstance(item, dict) and provider == "local" else item.get("id") if isinstance(item, dict) else item
            if value:
                models.append(str(value))
        return list(dict.fromkeys(models))

    async def interpret(
        self,
        provider: str,
        base_url: str,
        model: str,
        api_key: str,
        query: str,
        vocabulary: list[str],
    ) -> dict[str, Any]:
        base = normalize_endpoint(provider, base_url, api_key)
        instruction = (
            "Return JSON only with key ranked_tags. Select tags only by exact copy "
            "from the supplied dictionary, in decreasing relevance. Never invent a tag.\n"
            f"dictionary={json.dumps(vocabulary, ensure_ascii=False)}\nquery={query}"
        )
        if provider == "local":
            url = f"{base}/api/chat"
            body = {"model": model, "stream": False, "format": "json", "messages": [{"role": "user", "content": instruction}]}
        else:
            url = f"{base}/chat/completions"
            body = {"model": model, "stream": False, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": instruction}]}
        response = await self.request_json("POST", url, body, api_key=api_key, cache=True)
        raw = response.get("message", {}).get("content", "") if provider == "local" else response.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError("AI provider returned invalid interpretation JSON.") from error
        if not isinstance(parsed, dict):
            raise ProviderError("AI provider returned invalid interpretation JSON.")
        return {
            "ranked_tags": parsed.get("ranked_tags", parsed.get("selected_tags", parsed.get("tags", []))),
            "provider": provider,
        }

    async def test(self, provider: str, base_url: str, model: str, api_key: str = "") -> dict[str, str]:
        models = await self.models(provider, base_url, api_key)
        if model and model not in models:
            raise ProviderError("The selected AI model was not found.", status_code=400)
        if not models and not model:
            raise ProviderError("No AI models were returned.", status_code=502)
        return {"provider": provider, "model": model or models[0]}

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._inflight.values())
            self._inflight.clear()
            client = self._client
            self._client = None
            self._cache.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await client.aclose()

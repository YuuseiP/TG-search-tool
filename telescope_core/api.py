"""FastAPI composition for the TeleScope core services."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import AiModelsIn, AiSearchIn, AiSettingsIn, AiTestIn, BookmarkIn, CodeIn, SearchGroupIn, SearchIn, SettingsIn, SyncIn
from .observability import configure_runtime_logging, network_logger
from .services import ApiError, TeleScopeService
from .storage import Storage


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"


def create_app(data_dir: str | Path | None = None, telegram_gateway: Any = None, ai_gateway: Any = None) -> FastAPI:
    """Build an isolated application with injectable gateways and data directory."""

    store = Storage(data_dir or DEFAULT_DATA_DIR)
    runtime_log = configure_runtime_logging(store.root)
    service = TeleScopeService(store, telegram_gateway=telegram_gateway, ai_gateway=ai_gateway)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    application = FastAPI(title="TeleScope", lifespan=lifespan)
    application.state.service = service
    application.state.runtime_log = runtime_log
    if STATIC_DIR.is_dir():
        application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.exception_handler(ApiError)
    async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    @application.middleware("http")
    async def log_api_request(request: Request, call_next: Any) -> Response:
        if not request.url.path.startswith("/api/"):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                # The UI owns connection timeouts. Never leave an old JS bundle
                # cached after its request policy has been updated.
                response.headers["Cache-Control"] = "no-store"
            return response
        if request.url.path == "/api/client-events":
            return await call_next(request)
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            network_logger().exception("api_failure method=%s path=%s duration_ms=%d", request.method, request.url.path, (perf_counter() - started) * 1000)
            raise
        network_logger().info("api_request method=%s path=%s status=%s duration_ms=%d", request.method, request.url.path, response.status_code, (perf_counter() - started) * 1000)
        return response

    @application.get("/", include_in_schema=False)
    async def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/status")
    async def status(request: Request, restore: bool = False) -> dict[str, Any]:
        return await request.app.state.service.status(restore=restore)

    @application.post("/api/client-events", status_code=204)
    async def client_event(request: Request, payload: dict[str, Any]) -> Response:
        path = str(payload.get("path") or "")[:160]
        method = str(payload.get("method") or "")[:12]
        kind = str(payload.get("kind") or "")[:32]
        duration = int(payload.get("duration_ms") or 0)
        network_logger().warning("browser_network_event method=%s path=%s kind=%s duration_ms=%d", method, path, kind, max(0, min(duration, 3_600_000)))
        return Response(status_code=204)

    @application.get("/api/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        return await request.app.state.service.get_settings()

    @application.post("/api/settings")
    async def post_settings(request: Request, payload: SettingsIn) -> dict[str, bool]:
        return await request.app.state.service.save_settings(payload)

    @application.post("/api/auth/send-code")
    async def send_code(request: Request) -> dict[str, bool]:
        return await request.app.state.service.send_code()

    @application.post("/api/auth/verify")
    async def verify_code(request: Request, payload: CodeIn) -> dict[str, bool]:
        return await request.app.state.service.verify_code(payload)

    @application.get("/api/dialogs")
    async def dialogs(request: Request) -> list[dict[str, Any]]:
        return await request.app.state.service.dialogs()

    @application.post("/api/channels/scan")
    async def scan_channels(request: Request) -> dict[str, Any]:
        return await request.app.state.service.scan_channels()

    @application.get("/api/channels")
    async def channels(request: Request) -> list[dict[str, Any]]:
        return await request.app.state.service.channels()

    @application.get("/api/search-groups")
    async def search_groups(request: Request) -> list[dict[str, Any]]:
        return await request.app.state.service.search_groups()

    @application.post("/api/search-groups", status_code=201)
    async def create_search_group(request: Request, payload: SearchGroupIn) -> dict[str, Any]:
        return await request.app.state.service.create_search_group(payload)

    @application.put("/api/search-groups/{group_id}")
    async def update_search_group(request: Request, group_id: str, payload: SearchGroupIn) -> dict[str, Any]:
        return await request.app.state.service.update_search_group(group_id, payload)

    @application.delete("/api/search-groups/{group_id}")
    async def delete_search_group(request: Request, group_id: str) -> dict[str, bool]:
        return await request.app.state.service.delete_search_group(group_id)

    @application.delete("/api/channels/{channel_id}")
    async def delete_channel(request: Request, channel_id: int) -> dict[str, Any]:
        return await request.app.state.service.delete_channel(channel_id)


    @application.post("/api/sync", status_code=202)
    async def post_sync(request: Request, payload: SyncIn) -> dict[str, Any]:
        return await request.app.state.service.create_sync(payload)

    @application.post("/api/sync/incremental", status_code=202)
    async def post_incremental_sync(request: Request) -> dict[str, Any]:
        result = await request.app.state.service.create_incremental_sync()
        return result or {"state": "completed", "channels_total": 0, "channels_completed": 0, "messages_processed": 0, "mode": "incremental"}

    @application.post("/api/channels/{channel_id}/verify", status_code=202)
    async def verify_channel(request: Request, channel_id: int) -> dict[str, Any]:
        return await request.app.state.service.create_verification_sync(channel_id)

    @application.get("/api/sync")
    async def get_sync(request: Request) -> dict[str, Any]:
        return await request.app.state.service.sync_jobs()

    @application.get("/api/sync/{job_id}")
    async def get_sync_job(request: Request, job_id: str) -> dict[str, Any]:
        return await request.app.state.service.sync_status(job_id)

    @application.delete("/api/sync/{job_id}")
    async def delete_sync_job(request: Request, job_id: str) -> dict[str, Any]:
        return await request.app.state.service.delete_sync_job(job_id)

    @application.post("/api/search")
    async def search(request: Request, payload: SearchIn) -> dict[str, Any]:
        return await request.app.state.service.search(payload)

    @application.post("/api/ai/search")
    async def ai_search(request: Request, payload: AiSearchIn) -> dict[str, Any]:
        return await request.app.state.service.ai_search(payload)

    @application.get("/api/tags")
    async def tags(request: Request) -> dict[str, Any]:
        return request.app.state.service.store.tag_dictionary()

    @application.get("/api/ad-filters")
    async def get_ad_filters(request: Request) -> dict[str, Any]:
        return request.app.state.service.ad_filter_settings()

    @application.put("/api/ad-filters")
    async def put_ad_filters(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        return await request.app.state.service.save_ad_filter_settings(payload)

    @application.get("/api/ai/settings")
    async def get_ai_settings(request: Request) -> dict[str, Any]:
        return await request.app.state.service.get_ai_settings()

    @application.put("/api/ai/settings")
    async def put_ai_settings(request: Request, payload: AiSettingsIn) -> dict[str, Any]:
        return await request.app.state.service.save_ai_settings(payload)

    @application.post("/api/ai/models")
    async def ai_models(request: Request, payload: AiModelsIn) -> dict[str, Any]:
        return await request.app.state.service.list_ai_models(payload)

    @application.post("/api/ai/test")
    async def ai_test(request: Request, payload: AiTestIn | None = None) -> dict[str, str]:
        return await request.app.state.service.test_ai(payload)

    @application.post("/api/messages/{message_id}/bookmark")
    async def bookmark(request: Request, message_id: int, payload: BookmarkIn) -> dict[str, bool]:
        return await request.app.state.service.bookmark(message_id, payload.bookmarked)

    @application.get("/api/messages/{message_id}/preview")
    async def message_preview(request: Request, message_id: int) -> Response:
        content, media_type = await request.app.state.service.image_preview(message_id)
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "no-store"})

    @application.get("/api/messages/{message_id}/thumbnail")
    async def message_thumbnail(request: Request, message_id: int) -> Response:
        content, media_type = await request.app.state.service.video_thumbnail(message_id)
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "no-store"})

    return application


app = create_app()

"""Data contracts shared by the TeleScope storage, services, and API layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field


class SettingsIn(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = ""
    phone: str = Field(min_length=4)
    proxy_type: Literal["socks5", "http"] = "socks5"
    proxy_host: str = Field(default="127.0.0.1", min_length=1)
    proxy_port: int = Field(default=7890, ge=1, le=65535)
    proxy_username: str | None = None
    proxy_password: str | None = None


class CodeIn(BaseModel):
    code: str = Field(min_length=1)
    password: str | None = None


class SyncIn(BaseModel):
    channel_ids: list[int] = Field(min_length=1)
    limit_per_channel: int | None = Field(default=2000, ge=1, le=10000)
    mode: Literal["full", "incremental", "verify"] = "full"


class SearchIn(BaseModel):
    query: str = ""
    channel_ids: list[int] = Field(default_factory=list)
    media_types: list[Literal["text", "image", "video", "document", "audio", "link"]] = Field(default_factory=list)
    from_date: str | None = None
    until_date: str | None = None
    sort_order: Literal["desc", "asc"] = "desc"
    bookmarked_only: bool = False
    limit: int = Field(default=80, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    selected_tags: list[str] = Field(default_factory=list)
    include_hidden_ads: bool = False


class AiSearchIn(SearchIn):
    use_ai: bool = True
    minimum_results: int = Field(default=5, ge=0, le=200)


class AiSettingsIn(BaseModel):
    mode: Literal["disabled", "local", "cloud", "hybrid"] = "disabled"
    local_base_url: str = "http://127.0.0.1:11434"
    local_model: str = ""
    cloud_base_url: str = ""
    cloud_model: str = ""
    cloud_api_key: str = ""


class AiModelsIn(BaseModel):
    provider: Literal["local", "cloud"]
    base_url: str = Field(min_length=1)
    api_key: str = ""


class AiTestIn(AiModelsIn):
    model: str = ""


class BookmarkIn(BaseModel):
    bookmarked: bool


class SearchGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    channel_ids: list[int] = Field(min_length=1, max_length=500)


@dataclass
class SyncJob:
    """Durable progress state mirrored by the ``sync_jobs`` table."""

    job_id: str
    channel_ids: list[int]
    limit_per_channel: int | None
    mode: Literal["full", "incremental", "verify"] = "full"
    state: Literal["queued", "running", "completed", "failed"] = "queued"
    channels_total: int = 0
    channels_completed: int = 0
    messages_processed: int = 0
    current: dict[str, Any] | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    fingerprint: str = ""
    created_at: str | None = None
    updated_at: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "channels_total": self.channels_total,
            "channels_completed": self.channels_completed,
            "messages_processed": self.messages_processed,
            "mode": self.mode,
            "current": dict(self.current) if self.current else None,
            "results": [dict(item) for item in self.results],
            "error": self.error,
        }

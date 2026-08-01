from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
DB_PATH = DATA / "telescope.db"
CONFIG_PATH = DATA / "config.json"
DATA.mkdir(exist_ok=True)


@contextmanager
def database():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_db() -> None:
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT,
              kind TEXT NOT NULL DEFAULT 'channel', synced_at TEXT, message_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
              telegram_id INTEGER NOT NULL, sent_at TEXT NOT NULL, sender TEXT,
              text TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT 'text',
              has_link INTEGER NOT NULL DEFAULT 0, has_tag INTEGER NOT NULL DEFAULT 0,
              tags TEXT NOT NULL DEFAULT '', permalink TEXT, reply_to INTEGER,
              grouped_id TEXT, album_text TEXT NOT NULL DEFAULT '',
              UNIQUE(channel_id, telegram_id)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(text, tags, content='messages', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
              INSERT INTO message_search(rowid, text, tags) VALUES (new.id, new.text, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
              INSERT INTO message_search(message_search, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
              INSERT INTO message_search(message_search, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
              INSERT INTO message_search(rowid, text, tags) VALUES (new.id, new.text, new.tags);
            END;
            CREATE TABLE IF NOT EXISTS bookmarks (
              message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scanned_dialogs (
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT,
              kind TEXT NOT NULL, scanned_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
        if "grouped_id" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN grouped_id TEXT")
        if "album_text" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN album_text TEXT NOT NULL DEFAULT ''")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


TAG_RE = re.compile(r"(?<!\w)#([\w\u4e00-\u9fff-]+)")
URL_RE = re.compile(r"https?://|t\.me/", re.I)


def classify_media(message: Any) -> str:
    if not message.media:
        return "link" if URL_RE.search(message.message or "") else "text"
    if getattr(message, "video", False):
        return "video"
    if getattr(message, "photo", False):
        return "image"
    if getattr(message, "audio", False) or getattr(message, "voice", False):
        return "audio"
    return "document"


def telegram_permalink(username: str | None, channel_id: int, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{channel_id}/{message_id}"


class SettingsIn(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=8)
    phone: str = Field(min_length=4)
    proxy_type: Literal["socks5", "http"] = "socks5"
    proxy_host: str = Field(default="127.0.0.1", min_length=1)
    proxy_port: int = Field(default=7890, ge=1, le=65535)
    proxy_username: str | None = None
    proxy_password: str | None = None


class CodeIn(BaseModel):
    code: str = Field(min_length=3)
    password: str | None = None


class SyncIn(BaseModel):
    channel_ids: list[int] = Field(min_length=1)
    limit_per_channel: int = Field(default=2000, ge=1, le=10000)


class SearchIn(BaseModel):
    query: str = ""
    channel_ids: list[int] = []
    media_types: list[Literal["text", "image", "video", "document", "audio", "link"]] = []
    from_date: str | None = None
    until_date: str | None = None
    bookmarked_only: bool = False
    limit: int = Field(default=80, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class BookmarkIn(BaseModel):
    bookmarked: bool


@dataclass
class SyncJob:
    job_id: str
    channel_ids: list[int]
    limit_per_channel: int
    state: Literal["queued", "running", "completed", "failed"] = "queued"
    channels_total: int = 0
    channels_completed: int = 0
    messages_processed: int = 0
    current: dict[str, Any] | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


sync_jobs: dict[str, SyncJob] = {}
sync_task: asyncio.Task[None] | None = None


class TelegramService:
    client: Any = None
    phone_code_hash: str | None = None

    @staticmethod
    def proxy_from_config(config: dict[str, Any]) -> tuple[Any, ...]:
        proxy_type = config.get("proxy_type", "socks5")
        proxy_host = (config.get("proxy_host") or "127.0.0.1").strip()
        proxy_port = int(config.get("proxy_port") or 7890)
        kind = "socks5" if proxy_type == "socks5" else "http"
        username = (config.get("proxy_username") or "").strip() or None
        password = config.get("proxy_password") or None
        return (kind, proxy_host, proxy_port, username, password, True)

    @staticmethod
    def connection_error(exc: Exception) -> HTTPException:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError, OSError)):
            return HTTPException(503, "连接 Telegram 超时。请确认 Clash 已启动，且代理主机和端口与 Clash 配置一致。")
        return HTTPException(503, f"连接 Telegram 失败: {exc}")

    async def connect(self) -> Any:
        config = load_config()
        if not config.get("api_id") or not config.get("api_hash"):
            raise HTTPException(400, "请先填写 Telegram API ID 和 API Hash")
        from telethon import TelegramClient

        if not self.client:
            self.client = TelegramClient(
                str(DATA / "telegram"),
                int(config["api_id"]),
                config["api_hash"],
                proxy=self.proxy_from_config(config),
                connection_retries=2,
                retry_delay=1,
                timeout=12,
            )
        if not self.client.is_connected():
            try:
                await self.client.connect()
            except Exception as exc:
                with suppress(Exception):
                    await self.client.disconnect()
                self.client = None
                raise self.connection_error(exc) from exc
        return self.client

    async def status(self) -> dict[str, Any]:
        config = load_config()
        connected = False
        if config.get("api_id") and config.get("api_hash"):
            try:
                client = await self.connect()
                connected = await client.is_user_authorized()
            except Exception:
                connected = False
        return {"configured": bool(config.get("api_id")), "authorized": connected, "phone": config.get("phone", "")}

    async def send_code(self) -> bool:
        client = await self.connect()
        if await client.is_user_authorized():
            return False
        config = load_config()
        sent = await client.send_code_request(config["phone"])
        self.phone_code_hash = sent.phone_code_hash
        return True

    async def sign_in(self, code: str, password: str | None) -> None:
        client = await self.connect()
        config = load_config()
        try:
            await client.sign_in(phone=config["phone"], code=code, phone_code_hash=self.phone_code_hash)
        except Exception as exc:
            from telethon.errors import SessionPasswordNeededError
            if isinstance(exc, SessionPasswordNeededError) and password:
                await client.sign_in(password=password)
            elif isinstance(exc, SessionPasswordNeededError):
                raise HTTPException(400, "该账号开启了两步验证，请填写密码")
            else:
                raise HTTPException(400, f"登录失败: {exc}")

    async def scan_dialogs(self) -> list[dict[str, Any]]:
        client = await self.connect()
        if not await client.is_user_authorized():
            raise HTTPException(401, "请先登录 Telegram")
        items = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not (getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)):
                continue
            items.append({"id": entity.id, "title": dialog.name, "username": getattr(entity, "username", None), "kind": "channel" if getattr(entity, "broadcast", False) else "group"})
        return sorted(items, key=lambda item: item["title"].lower())

    async def sync(self, dialogs: list[dict[str, Any]], limit: int, job: SyncJob) -> None:
        client = await self.connect()
        if not await client.is_user_authorized():
            raise HTTPException(401, "请先登录 Telegram")
        for position, dialog in enumerate(dialogs, start=1):
            result = {"channel_id": dialog["id"], "title": dialog["title"], "state": "running", "processed": 0, "stored_total": 0, "error": None}
            job.results.append(result)
            job.current = {"channel_id": dialog["id"], "title": dialog["title"], "position": position, "processed": 0, "target": limit}
            count = 0
            try:
                entity = await client.get_entity(dialog["id"])
                with database() as db:
                    db.execute("INSERT INTO channels(id,title,username,kind) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,username=excluded.username,kind=excluded.kind", (dialog["id"], dialog["title"], dialog["username"], dialog["kind"]))
                async for msg in client.iter_messages(entity, limit=limit):
                    content = msg.message or ""
                    tags = " ".join(f"#{tag.lower()}" for tag in TAG_RE.findall(content))
                    media_type = classify_media(msg)
                    username = dialog["username"]
                    permalink = telegram_permalink(username, dialog["id"], msg.id)
                    grouped_id = str(getattr(msg, "grouped_id", None) or "") or None
                    album_text = content if grouped_id else ""
                    with database() as db:
                        db.execute("""INSERT INTO messages(channel_id,telegram_id,sent_at,sender,text,media_type,has_link,has_tag,tags,permalink,reply_to,grouped_id,album_text)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel_id,telegram_id) DO UPDATE SET text=excluded.text,media_type=excluded.media_type,has_link=excluded.has_link,has_tag=excluded.has_tag,tags=excluded.tags,permalink=excluded.permalink,reply_to=excluded.reply_to,grouped_id=excluded.grouped_id,album_text=CASE WHEN excluded.album_text <> '' THEN excluded.album_text ELSE messages.album_text END""", (dialog["id"], msg.id, utc_iso(msg.date), str(msg.sender_id or ""), content, media_type, int(bool(URL_RE.search(content))), int(bool(tags)), tags, permalink, getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None), grouped_id, album_text))
                        if grouped_id and content:
                            db.execute("UPDATE messages SET album_text=? WHERE channel_id=? AND grouped_id=?", (content, dialog["id"], grouped_id))
                    count += 1
                    job.messages_processed += 1
                    result["processed"] = count
                    job.current["processed"] = count
                    if count % 25 == 0:
                        await asyncio.sleep(0)
                with database() as db:
                    db.execute("UPDATE channels SET synced_at=?,message_count=(SELECT COUNT(*) FROM messages WHERE channel_id=?) WHERE id=?", (utc_iso(), dialog["id"], dialog["id"]))
                    result["stored_total"] = db.execute("SELECT COUNT(*) FROM messages WHERE channel_id=?", (dialog["id"],)).fetchone()[0]
                result["state"] = "completed"
            except Exception as exc:
                result["state"] = "failed"
                result["error"] = str(exc)
            finally:
                job.channels_completed += 1
        job.current = None


def scanned_dialogs(requested_ids: list[int] | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id,title,username,kind FROM scanned_dialogs"
    params: list[Any] = []
    if requested_ids is not None:
        marks = ",".join("?" for _ in requested_ids)
        sql += f" WHERE id IN ({marks})"
        params.extend(requested_ids)
    sql += " ORDER BY title COLLATE NOCASE"
    with database() as db:
        return [dict(row) for row in db.execute(sql, params)]


def snapshot(job: SyncJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "state": job.state,
        "channels_total": job.channels_total,
        "channels_completed": job.channels_completed,
        "messages_processed": job.messages_processed,
        "current": dict(job.current) if job.current else None,
        "results": [dict(result) for result in job.results],
        "error": job.error,
    }


async def run_sync_job(job: SyncJob) -> None:
    try:
        dialogs = scanned_dialogs(job.channel_ids)
        if len(dialogs) != len(set(job.channel_ids)):
            raise HTTPException(400, "所选频道不在扫描结果中，请先重新扫描频道列表")
        job.channels_total = len(dialogs)
        job.state = "running"
        await telegram.sync(dialogs, job.limit_per_channel, job)
        job.state = "completed" if all(item["state"] == "completed" for item in job.results) else "failed"
        if job.state == "failed":
            job.error = "部分频道同步失败，请查看频道列表中的错误信息"
    except Exception as exc:
        job.state = "failed"
        job.error = exc.detail if isinstance(exc, HTTPException) else str(exc)


async def shutdown_services() -> None:
    if sync_task and not sync_task.done():
        sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await sync_task
    if telegram.client:
        await telegram.client.disconnect()


telegram = TelegramService()


def fts_expression(text: str) -> str:
    # Quote individual tokens so punctuation and CJK text stay valid FTS input.
    terms = [term for term in re.split(r"\s+", text.strip()) if term and not term.startswith("#")]
    return " AND ".join(f'"{term.replace(chr(34), "")}"' for term in terms)


def search(payload: SearchIn) -> dict[str, Any]:
    tags = [tag.lower() if tag.startswith("#") else f"#{tag.lower()}" for tag in TAG_RE.findall(payload.query)]
    where, params = [], []
    join = ""
    expression = fts_expression(payload.query)
    if expression:
        join = " JOIN message_search ON message_search.rowid=m.id "
        where.append("message_search MATCH ?")
        params.append(expression)
    if tags:
        where.extend("m.tags LIKE ?" for _ in tags)
        params.extend(f"%{tag}%" for tag in tags)
    if payload.channel_ids:
        marks = ",".join("?" for _ in payload.channel_ids)
        where.append(f"m.channel_id IN ({marks})")
        params.extend(payload.channel_ids)
    if payload.media_types:
        marks = ",".join("?" for _ in payload.media_types)
        where.append(f"m.media_type IN ({marks})")
        params.extend(payload.media_types)
    if payload.from_date:
        where.append("m.sent_at >= ?")
        params.append(payload.from_date + "T00:00:00")
    if payload.until_date:
        where.append("m.sent_at <= ?")
        params.append(payload.until_date + "T23:59:59.999999")
    if payload.bookmarked_only:
        where.append("b.message_id IS NOT NULL")
    # Telegram albums are individual messages sharing grouped_id. Keep one
    # representative card per album, preferring the message that owns its caption.
    where.append("""(m.grouped_id IS NULL OR m.id = COALESCE(
        (SELECT album.id FROM messages album
         WHERE album.channel_id=m.channel_id AND album.grouped_id=m.grouped_id AND album.text <> ''
         ORDER BY album.id LIMIT 1),
        (SELECT album.id FROM messages album
         WHERE album.channel_id=m.channel_id AND album.grouped_id=m.grouped_id
         ORDER BY album.id LIMIT 1)
    ))""")
    clause = " WHERE " + " AND ".join(where) if where else ""
    sql = f"""SELECT m.*, c.title AS channel_title, c.username AS channel_username, b.message_id IS NOT NULL AS bookmarked
              FROM messages m {join} JOIN channels c ON c.id=m.channel_id
              LEFT JOIN bookmarks b ON b.message_id=m.id {clause}
              ORDER BY m.sent_at DESC LIMIT ? OFFSET ?"""
    with database() as db:
        rows = [dict(row) for row in db.execute(sql, [*params, payload.limit, payload.offset])]
        total = db.execute(f"SELECT COUNT(*) FROM messages m {join} LEFT JOIN bookmarks b ON b.message_id=m.id {clause}", params).fetchone()[0]
    return {"items": rows, "total": total}


def delete_channel_index(channel_id: int) -> dict[str, int | str | bool]:
    with database() as db:
        row = db.execute("SELECT id,title FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "未找到该频道的本地索引")
        message_count = db.execute("SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()[0]
        bookmark_count = db.execute("""SELECT COUNT(*) FROM bookmarks b
                                      JOIN messages m ON m.id=b.message_id
                                      WHERE m.channel_id=?""", (channel_id,)).fetchone()[0]
        db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    return {"deleted": True, "channel_id": channel_id, "title": row["title"], "messages_deleted": message_count, "bookmarks_deleted": bookmark_count}


def remove_legacy_demo_data() -> None:
    # Delete only the exact sample channels that earlier versions created.
    samples = ((1001, "产品观察", "productnotes"), (1002, "设计资料库", "designstash"), (1003, "开发者周刊", "devweekly"))
    with database() as db:
        for channel_id, title, username in samples:
            db.execute("DELETE FROM channels WHERE id=? AND title=? AND username=?", (channel_id, title, username))


def backfill_private_permalinks() -> None:
    with database() as db:
        db.execute("""UPDATE messages SET permalink='https://t.me/c/' || channel_id || '/' || telegram_id
                      WHERE (permalink IS NULL OR permalink='')
                        AND channel_id IN (SELECT id FROM channels WHERE username IS NULL)""")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_db()
    remove_legacy_demo_data()
    backfill_private_permalinks()
    yield
    await shutdown_services()


app = FastAPI(title="TeleScope", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return await telegram.status()


@app.post("/api/settings")
async def settings(payload: SettingsIn) -> dict[str, bool]:
    save_config(payload.model_dump())
    if telegram.client:
        await telegram.client.disconnect()
    telegram.client = None
    return {"saved": True}


@app.post("/api/auth/send-code")
async def send_code() -> dict[str, bool]:
    sent = await telegram.send_code()
    return {"sent": sent, "authorized": not sent}


@app.post("/api/auth/verify")
async def verify_code(payload: CodeIn) -> dict[str, bool]:
    await telegram.sign_in(payload.code, payload.password)
    return {"authorized": True}


@app.get("/api/dialogs")
async def dialogs() -> list[dict[str, Any]]:
    return scanned_dialogs()


@app.post("/api/channels/scan")
async def scan_channels() -> dict[str, Any]:
    items = await telegram.scan_dialogs()
    with database() as db:
        db.execute("DELETE FROM scanned_dialogs")
        db.executemany(
            "INSERT INTO scanned_dialogs(id,title,username,kind,scanned_at) VALUES(?,?,?,?,?)",
            [(item["id"], item["title"], item["username"], item["kind"], utc_iso()) for item in items],
        )
    return {"items": items, "count": len(items)}


@app.get("/api/channels")
async def channels() -> list[dict[str, Any]]:
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM channels ORDER BY title COLLATE NOCASE")]


@app.delete("/api/channels/{channel_id}")
async def delete_channel(channel_id: int) -> dict[str, int | str | bool]:
    if sync_task and not sync_task.done():
        raise HTTPException(409, "同步任务正在运行，完成后再删除索引")
    return delete_channel_index(channel_id)


@app.post("/api/sync", status_code=202)
async def sync(payload: SyncIn) -> dict[str, str]:
    global sync_task
    if sync_task and not sync_task.done():
        raise HTTPException(409, "已有同步任务正在运行，请等待其完成")
    while len(sync_jobs) >= 20:
        stale_id = next((job_id for job_id, job in sync_jobs.items() if job.state in {"completed", "failed"}), None)
        if not stale_id:
            break
        sync_jobs.pop(stale_id)
    job = SyncJob(job_id=uuid4().hex, channel_ids=list(dict.fromkeys(payload.channel_ids)), limit_per_channel=payload.limit_per_channel)
    sync_jobs[job.job_id] = job
    sync_task = asyncio.create_task(run_sync_job(job))
    return {"job_id": job.job_id, "state": job.state}


@app.get("/api/sync/{job_id}")
async def sync_status(job_id: str) -> dict[str, Any]:
    job = sync_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "同步任务不存在或服务已重启")
    return snapshot(job)


@app.post("/api/search")
async def search_messages(payload: SearchIn) -> dict[str, Any]:
    return search(payload)


@app.post("/api/messages/{message_id}/bookmark")
async def bookmark(message_id: int, payload: BookmarkIn) -> dict[str, bool]:
    with database() as db:
        if not db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
            raise HTTPException(404, "未找到消息")
        if payload.bookmarked:
            db.execute("INSERT OR IGNORE INTO bookmarks(message_id,created_at) VALUES(?,?)", (message_id, utc_iso()))
        else:
            db.execute("DELETE FROM bookmarks WHERE message_id=?", (message_id,))
    return {"bookmarked": payload.bookmarked}

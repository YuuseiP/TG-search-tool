"""SQLite persistence and deterministic local search for TeleScope."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import SearchIn, SyncJob


TAG_RE = re.compile(r"(?<!\w)#([\w\u4e00-\u9fff-]+)")
AD_URL_RE = re.compile(r"https?://[^\s?#]+(?:\?[^\s#]*)?", re.IGNORECASE)


def advertisement_fingerprint(text: str) -> str:
    """Normalize repeat posts without changing the archived original text."""

    normalized = AD_URL_RE.sub(lambda match: match.group(0).split("?", 1)[0], text or "")
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""
URL_RE = re.compile(r"https?://|t\.me/", re.IGNORECASE)
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def normalize_tag(value: str) -> str:
    clean = str(value or "").strip().lower()
    if not clean:
        return ""
    return clean if clean.startswith("#") else f"#{clean}"


def extract_tags(text: str) -> list[str]:
    return list(dict.fromkeys(f"#{value.lower()}" for value in TAG_RE.findall(text or "")))


def telegram_permalink(username: str | None, channel_id: int, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{channel_id}/{message_id}"


def _read_value(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _message_date(value: Any) -> str:
    if isinstance(value, datetime):
        return utc_iso(value)
    if value:
        return str(value)
    return utc_iso()


def _rich_text(value: Any) -> str:
    """Flatten Telegram RichMessage text nodes without importing Telethon types."""

    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_rich_text(item) for item in value)
    if value is None:
        return ""
    for name in ("text", "texts"):
        child = _read_value(value, name, default=None)
        if child is not None and child is not value:
            return _rich_text(child)
    return ""


def _rich_message_text(message: Any) -> str:
    rich = _read_value(message, "rich_message", default=None)
    blocks = _read_value(rich, "blocks", default=[]) if rich is not None else []
    if not isinstance(blocks, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in blocks:
        for name in ("text", "caption", "author", "title", "subtitle", "footer", "credit"):
            value = _rich_text(_read_value(block, name, default=None)).strip()
            if value:
                parts.append(value)
    return "\n".join(parts)


def _document_media_type(document: Any) -> str:
    mime_type = str(_read_value(document, "mime_type", default="") or "").casefold()
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    attributes = _read_value(document, "attributes", default=[])
    for attribute in attributes if isinstance(attributes, (list, tuple)) else []:
        name = type(attribute).__name__.casefold()
        kind = str(_read_value(attribute, "_", "type", "kind", default="") or "").casefold()
        if "video" in name or "video" in kind:
            return "video"
        if "audio" in name or "audio" in kind:
            return "audio"
    return "document"


def _media_type(message: Any, text: str) -> str:
    explicit = _read_value(message, "media_type", default=None)
    if explicit in {"text", "image", "video", "document", "audio", "link"}:
        return explicit
    rich = _read_value(message, "rich_message", default=None)
    if _read_value(rich, "photos", default=[]):
        return "image"
    documents = _read_value(rich, "documents", default=[])
    if isinstance(documents, (list, tuple)) and documents:
        return _document_media_type(documents[0])
    if not _read_value(message, "media", default=None):
        return "link" if URL_RE.search(text) else "text"
    if _read_value(message, "video", default=False):
        return "video"
    if _read_value(message, "photo", default=False):
        return "image"
    if _read_value(message, "audio", "voice", default=False):
        return "audio"
    return "document"


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  username TEXT,
  kind TEXT NOT NULL DEFAULT 'channel',
  synced_at TEXT,
  message_count INTEGER NOT NULL DEFAULT 0,
  full_history_synced INTEGER NOT NULL DEFAULT 0,
  auto_update_enabled INTEGER NOT NULL DEFAULT 0,
  last_telegram_id INTEGER,
  last_incremental_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  telegram_id INTEGER NOT NULL,
  sent_at TEXT NOT NULL,
  sender TEXT,
  text TEXT NOT NULL DEFAULT '',
  media_type TEXT NOT NULL DEFAULT 'text',
  has_link INTEGER NOT NULL DEFAULT 0,
  has_tag INTEGER NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '',
  permalink TEXT,
  grouped_id TEXT,
  album_text TEXT NOT NULL DEFAULT '',
  ad_fingerprint TEXT NOT NULL DEFAULT '',
  UNIQUE(channel_id, telegram_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS message_search
  USING fts5(text, tags, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO message_search(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO message_search(message_search, rowid, text, tags)
  VALUES ('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO message_search(message_search, rowid, text, tags)
  VALUES ('delete', old.id, old.text, old.tags);
  INSERT INTO message_search(rowid, text, tags)
  VALUES (new.id, new.text, new.tags);
END;
CREATE TABLE IF NOT EXISTS bookmarks (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scanned_dialogs (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  username TEXT,
  kind TEXT NOT NULL,
  scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_tags (
  tag TEXT PRIMARY KEY,
  message_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tag_dictionary_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_jobs (
  job_id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  channel_ids TEXT NOT NULL,
  limit_per_channel INTEGER,
  sync_mode TEXT NOT NULL DEFAULT 'full',
  state TEXT NOT NULL,
  channels_total INTEGER NOT NULL DEFAULT 0,
  channels_completed INTEGER NOT NULL DEFAULT 0,
  messages_processed INTEGER NOT NULL DEFAULT 0,
  current TEXT,
  results TEXT NOT NULL DEFAULT '[]',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sync_jobs_fingerprint_idx
  ON sync_jobs(fingerprint, updated_at DESC);
CREATE INDEX IF NOT EXISTS messages_album_group_idx
  ON messages(channel_id, grouped_id, id);
"""


class Storage:
    """A per-application SQLite store; it never touches the default data path on import."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).expanduser().resolve()
        self.db_path = self.root / "telescope.db"
        self.config_path = self.root / "config.json"
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        """Serialize schema work across launch processes without stale lock files."""

        lock_path = self.root / ".telescope.initialize.lock"
        deadline = time.monotonic() + 15
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for another TeleScope startup to finish.") from error
                    time.sleep(0.1)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with self._lock:
            db = self._connect()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def initialize(self) -> None:
        if self._initialized and self.db_path.exists():
            return
        with self._lock:
            if self._initialized and self.db_path.exists():
                return
            self.root.mkdir(parents=True, exist_ok=True)
            # A normal restart must not take a write lock just to restate an
            # already-current schema. This is especially important while a
            # long-running archive process still has SQLite open.
            if self.db_path.exists():
                db = self._connect()
                try:
                    if self._has_current_schema(db):
                        self._reconcile_full_history_flags(db)
                        db.commit()
                        self._initialized = True
                        return
                finally:
                    db.close()
            with self._initialization_lock():
                db = self._connect()
                try:
                    db.executescript(SCHEMA)
                    self._migrate_columns(db)
                    # These tables belong to the removed per-message AI index. Ordinary
                    # archive tables and their FTS/bookmark data are left untouched.
                    db.execute("DROP INDEX IF EXISTS ai_documents_status_idx")
                    db.execute("DROP INDEX IF EXISTS ai_index_jobs_state_idx")
                    db.execute("DROP TABLE IF EXISTS ai_documents")
                    db.execute("DROP TABLE IF EXISTS ai_index_jobs")
                    message_count = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
                    search_count = int(db.execute("SELECT COUNT(*) FROM message_search").fetchone()[0])
                    if message_count != search_count:
                        db.execute("INSERT INTO message_search(message_search) VALUES ('rebuild')")
                    has_tag_metadata = db.execute(
                        "SELECT 1 FROM tag_dictionary_meta WHERE id=1"
                    ).fetchone()
                    if not has_tag_metadata:
                        self._rebuild_tags(db)
                    db.commit()
                    self._initialized = True
                finally:
                    db.close()

    @staticmethod
    def _has_current_schema(db: sqlite3.Connection) -> bool:
        required_tables = {
            "channels", "messages", "message_search", "bookmarks", "scanned_dialogs",
            "local_tags", "tag_dictionary_meta", "sync_jobs",
        }
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required_tables.issubset(tables) or any(name.startswith("ai_") for name in tables):
            return False
        columns = {
            "messages": {"grouped_id", "album_text", "ad_fingerprint"},
            "channels": {"message_count", "full_history_synced", "auto_update_enabled", "last_telegram_id", "last_incremental_at"},
            "sync_jobs": {"sync_mode"},
        }
        for table, expected in columns.items():
            actual = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
            if not expected.issubset(actual):
                return False
        indexes = {str(row[1]) for row in db.execute("PRAGMA index_list(messages)")}
        if "messages_album_group_idx" not in indexes:
            return False
        metadata = db.execute("SELECT 1 FROM tag_dictionary_meta WHERE id=1").fetchone()
        if not metadata:
            return False
        message_count = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        search_count = int(db.execute("SELECT COUNT(*) FROM message_search").fetchone()[0])
        return message_count == search_count

    @staticmethod
    def _migrate_columns(db: sqlite3.Connection) -> None:
        def columns(table: str) -> set[str]:
            return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}

        message_columns = columns("messages")
        if "grouped_id" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN grouped_id TEXT")
        if "album_text" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN album_text TEXT NOT NULL DEFAULT ''")
        if "ad_fingerprint" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN ad_fingerprint TEXT NOT NULL DEFAULT ''")
            rows = db.execute("SELECT id,text FROM messages").fetchall()
            db.executemany("UPDATE messages SET ad_fingerprint=? WHERE id=?", [(advertisement_fingerprint(row[1]), row[0]) for row in rows])
        db.execute("CREATE INDEX IF NOT EXISTS messages_ad_fingerprint_idx ON messages(channel_id, ad_fingerprint, sent_at DESC, id DESC)")
        channel_columns = columns("channels")
        if "message_count" not in channel_columns:
            db.execute("ALTER TABLE channels ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0")
        if "full_history_synced" not in channel_columns:
            db.execute("ALTER TABLE channels ADD COLUMN full_history_synced INTEGER NOT NULL DEFAULT 0")
        if "auto_update_enabled" not in channel_columns:
            db.execute("ALTER TABLE channels ADD COLUMN auto_update_enabled INTEGER NOT NULL DEFAULT 0")
        if "last_telegram_id" not in channel_columns:
            db.execute("ALTER TABLE channels ADD COLUMN last_telegram_id INTEGER")
        if "last_incremental_at" not in channel_columns:
            db.execute("ALTER TABLE channels ADD COLUMN last_incremental_at TEXT")
        sync_columns = columns("sync_jobs")
        if "sync_mode" not in sync_columns:
            db.execute("ALTER TABLE sync_jobs ADD COLUMN sync_mode TEXT NOT NULL DEFAULT 'full'")
        Storage._reconcile_full_history_flags(db)

    @staticmethod
    def _reconcile_full_history_flags(db: sqlite3.Connection) -> None:
        """Derive auto-update eligibility only from completed unbounded runs."""

        completed = db.execute(
            "SELECT channel_ids,sync_mode FROM sync_jobs WHERE state='completed' AND limit_per_channel IS NULL "
            "AND sync_mode IN ('full','verify')"
        ).fetchall()
        full_history_ids: set[int] = set()
        for row in completed:
            try:
                channel_ids = [int(value) for value in json.loads(row[0] or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for channel_id in channel_ids:
                full_history_ids.add(channel_id)
        for channel_id in full_history_ids:
            last = db.execute("SELECT MAX(telegram_id) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()[0]
            db.execute(
                "UPDATE channels SET full_history_synced=1,auto_update_enabled=1,last_telegram_id=COALESCE(last_telegram_id,?),"
                "last_incremental_at=COALESCE(last_incremental_at,synced_at) WHERE id=?",
                (last, channel_id),
            )
        if full_history_ids:
            marks = ",".join("?" for _ in full_history_ids)
            db.execute(
                f"UPDATE channels SET full_history_synced=0,auto_update_enabled=0 "
                f"WHERE id NOT IN ({marks}) AND (full_history_synced=1 OR auto_update_enabled=1)",
                tuple(full_history_ids),
            )
        else:
            db.execute("UPDATE channels SET full_history_synced=0,auto_update_enabled=0 WHERE full_history_synced=1 OR auto_update_enabled=1")

    def load_config(self) -> dict[str, Any]:
        self.initialize()
        if not self.config_path.exists():
            return {}
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def save_config(self, value: dict[str, Any]) -> None:
        self.initialize()
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, indent=2)
        with self._lock:
            fd, temp_name = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.config_path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def _tag_rows(self, db: sqlite3.Connection) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for row in db.execute("SELECT tags FROM messages WHERE tags <> ''"):
            for raw_tag in str(row[0]).split():
                tag = normalize_tag(raw_tag)
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda item: item[0].casefold())

    def _dictionary_version(self, rows: list[tuple[str, int]]) -> str:
        encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def _rebuild_tags(self, db: sqlite3.Connection) -> str:
        rows = self._tag_rows(db)
        version = self._dictionary_version(rows)
        previous = db.execute("SELECT version FROM tag_dictionary_meta WHERE id=1").fetchone()
        if not previous or previous[0] != version:
            db.execute("DELETE FROM local_tags")
            db.executemany(
                "INSERT INTO local_tags(tag,message_count,updated_at) VALUES(?,?,?)",
                [(tag, count, utc_iso()) for tag, count in rows],
            )
            db.execute(
                "INSERT INTO tag_dictionary_meta(id,version,updated_at) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at",
                (version, utc_iso()),
            )
        return version

    def tag_dictionary(self) -> dict[str, Any]:
        with self.transaction() as db:
            version = self._rebuild_tags(db)
            rows = db.execute(
                "SELECT tag,message_count FROM local_tags ORDER BY tag COLLATE NOCASE"
            ).fetchall()
        return {
            "dictionary_version": version,
            "tags": [row[0] for row in rows],
            "items": [row[0] for row in rows],
            "counts": {row[0]: row[1] for row in rows},
        }

    def save_scanned_dialogs(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = utc_iso()
        with self.transaction() as db:
            db.execute("DELETE FROM scanned_dialogs")
            for item in items:
                db.execute(
                    "INSERT INTO scanned_dialogs(id,title,username,kind,scanned_at) VALUES(?,?,?,?,?)",
                    (int(item["id"]), str(item.get("title") or item["id"]), item.get("username"), item.get("kind", "channel"), now),
                )
        return self.scanned_dialogs()

    def scanned_dialogs(self, requested_ids: list[int] | None = None) -> list[dict[str, Any]]:
        with self.transaction() as db:
            sql = "SELECT sd.id,sd.title,sd.username,sd.kind FROM scanned_dialogs sd"
            params: list[Any] = []
            if requested_ids is not None:
                if not requested_ids:
                    return []
                marks = ",".join("?" for _ in requested_ids)
                sql += f" WHERE sd.id IN ({marks})"
                params.extend(requested_ids)
            sql += " ORDER BY sd.title COLLATE NOCASE"
            return [dict(row) for row in db.execute(sql, params)]

    def channels(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT c.id,c.title,c.username,c.kind,c.synced_at,c.full_history_synced,c.auto_update_enabled,c.last_telegram_id,c.last_incremental_at,"
                "(SELECT COUNT(*) FROM messages m WHERE m.channel_id=c.id) AS message_count "
                "FROM channels c "
                "ORDER BY c.title COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def indexed_channels(self, requested_ids: list[int] | None = None) -> list[dict[str, Any]]:
        with self.transaction() as db:
            sql = "SELECT c.id,c.title,c.username,c.kind FROM channels c"
            params: list[Any] = []
            if requested_ids is not None:
                if not requested_ids:
                    return []
                marks = ",".join("?" for _ in requested_ids)
                sql += f" WHERE c.id IN ({marks})"
                params.extend(requested_ids)
            sql += " ORDER BY c.title COLLATE NOCASE"
            return [dict(row) for row in db.execute(sql, params)]

    def auto_update_channel_ids(self) -> list[int]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT c.id FROM channels c WHERE c.full_history_synced=1 AND c.auto_update_enabled=1 "
                "ORDER BY c.title COLLATE NOCASE"
            ).fetchall()
            return [int(row[0]) for row in rows]

    def channels_for_id(self, channel_id: int) -> int:
        with self.transaction() as db:
            row = db.execute("SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()
            return int(row[0])

    def telegram_message_ids(self, channel_id: int) -> set[int]:
        """Return the locally archived Telegram IDs for one source."""

        with self.transaction() as db:
            rows = db.execute(
                "SELECT telegram_id FROM messages WHERE channel_id=?", (channel_id,)
            ).fetchall()
        return {int(row[0]) for row in rows}

    def verify_message_id_order(self, channel_id: int) -> dict[str, int | bool | None]:
        """Validate local Telegram message IDs without assuming deleted IDs are gaps."""

        with self.transaction() as db:
            rows = db.execute(
                "SELECT telegram_id FROM messages WHERE channel_id=? ORDER BY telegram_id ASC", (channel_id,)
            ).fetchall()
        previous: int | None = None
        for row in rows:
            current = int(row[0])
            if previous is not None and current <= previous:
                return {"verified": False, "message_count": len(rows), "first_id": rows[0][0] if rows else None, "last_id": current}
            previous = current
        return {
            "verified": True,
            "message_count": len(rows),
            "first_id": int(rows[0][0]) if rows else None,
            "last_id": int(rows[-1][0]) if rows else None,
        }

    def sync_cursor(self, channel_id: int) -> int:
        with self.transaction() as db:
            row = db.execute("SELECT last_telegram_id FROM channels WHERE id=?", (channel_id,)).fetchone()
            return int(row[0] or 0) if row else 0

    def sync_bounds(self, channel_id: int) -> dict[str, int | bool] | None:
        """Describe the archived ID interval used to skip known history windows."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT MIN(m.telegram_id),MAX(m.telegram_id),c.full_history_synced "
                "FROM channels c LEFT JOIN messages m ON m.channel_id=c.id WHERE c.id=? GROUP BY c.id",
                (channel_id,),
            ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None
        return {"first_id": int(row[0]), "last_id": int(row[1]), "complete": bool(row[2])}

    def mark_channel_sync_completed(self, channel_id: int, *, completed_full_history: bool) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT MAX(telegram_id) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()
            last_telegram_id = int(row[0] or 0)
            if completed_full_history:
                db.execute(
                    "UPDATE channels SET synced_at=?,full_history_synced=1,auto_update_enabled=1,last_telegram_id=?,last_incremental_at=? WHERE id=?",
                    (utc_iso(), last_telegram_id or None, utc_iso(), channel_id),
                )
            else:
                db.execute(
                    "UPDATE channels SET synced_at=?,last_telegram_id=MAX(COALESCE(last_telegram_id,0),?),last_incremental_at=? WHERE id=?",
                    (utc_iso(), last_telegram_id, utc_iso(), channel_id),
                )

    def _upsert_channel(self, db: sqlite3.Connection, dialog: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO channels(id,title,username,kind) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title,username=excluded.username,kind=excluded.kind",
            (int(dialog["id"]), str(dialog.get("title") or dialog["id"]), dialog.get("username"), dialog.get("kind", "channel")),
        )

    def _upsert_message(self, db: sqlite3.Connection, dialog: dict[str, Any], message: Any) -> int | None:
        telegram_id = _read_value(message, "telegram_id", "message_id", "id", default=None)
        if telegram_id is None:
            return None
        telegram_id = int(telegram_id)
        text = str(_read_value(message, "text", "message", default="") or "")
        if not text:
            text = _rich_message_text(message)
        fingerprint = advertisement_fingerprint(text)
        tags = extract_tags(text)
        grouped_id = _read_value(message, "grouped_id", default=None)
        grouped_id = str(grouped_id) if grouped_id else None
        album_text = str(_read_value(message, "album_text", default="") or "")
        sender = _read_value(message, "sender", "sender_id", default=None)
        sender = str(sender) if sender is not None else None
        media_type = _media_type(message, text)
        permalink = telegram_permalink(dialog.get("username"), int(dialog["id"]), telegram_id)
        db.execute(
            "INSERT INTO messages(channel_id,telegram_id,sent_at,sender,text,media_type,has_link,has_tag,tags,permalink,grouped_id,album_text,ad_fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(channel_id,telegram_id) DO UPDATE SET sent_at=excluded.sent_at,sender=excluded.sender,"
            # Telegram can briefly return an empty placeholder for a message
            # while media/caption data is unavailable. Do not let that
            # incremental response erase a previously archived payload.
            "text=CASE WHEN excluded.text <> '' THEN excluded.text ELSE messages.text END,"
            "media_type=CASE WHEN excluded.text <> '' OR excluded.media_type <> 'text' THEN excluded.media_type ELSE messages.media_type END,"
            "has_link=CASE WHEN excluded.text <> '' THEN excluded.has_link ELSE messages.has_link END,"
            "has_tag=CASE WHEN excluded.text <> '' THEN excluded.has_tag ELSE messages.has_tag END,"
            "tags=CASE WHEN excluded.text <> '' THEN excluded.tags ELSE messages.tags END,permalink=excluded.permalink,grouped_id=COALESCE(excluded.grouped_id,messages.grouped_id),"
            "album_text=CASE WHEN excluded.album_text <> '' THEN excluded.album_text ELSE messages.album_text END,ad_fingerprint=excluded.ad_fingerprint",
            (
                int(dialog["id"]), telegram_id, _message_date(_read_value(message, "sent_at", "date", default=None)),
                sender, text, media_type, int(bool(URL_RE.search(text))), int(bool(tags)), " ".join(tags),
                permalink, grouped_id, album_text, fingerprint,
            ),
        )
        row = db.execute(
            "SELECT id FROM messages WHERE channel_id=? AND telegram_id=?", (int(dialog["id"]), telegram_id)
        ).fetchone()
        return int(row[0]) if row else None

    def upsert_messages(self, dialog: dict[str, Any], messages: Iterable[Any], *, refresh_tags: bool = True) -> dict[str, int]:
        observed = 0
        with self.transaction() as db:
            self._upsert_channel(db, dialog)
            for message in messages:
                if self._upsert_message(db, dialog, message) is not None:
                    observed += 1
            grouped_rows = db.execute(
                "SELECT grouped_id,MAX(album_text) FROM messages WHERE channel_id=? AND grouped_id IS NOT NULL AND album_text<>'' GROUP BY grouped_id",
                (int(dialog["id"]),),
            ).fetchall()
            for grouped_id, caption in grouped_rows:
                db.execute(
                    "UPDATE messages SET album_text=? WHERE channel_id=? AND grouped_id=?",
                    (caption, int(dialog["id"]), grouped_id),
                )
            stored_total = db.execute(
                "SELECT COUNT(*) FROM messages WHERE channel_id=?", (int(dialog["id"]),)
            ).fetchone()[0]
            db.execute(
                "UPDATE channels SET synced_at=?,message_count=? WHERE id=?",
                (utc_iso(), stored_total, int(dialog["id"])),
            )
            if refresh_tags:
                self._rebuild_tags(db)
        return {"observed": observed, "stored_total": int(stored_total)}

    def refresh_tag_dictionary(self) -> None:
        """Rebuild tag counts once after a batched import, not for every message."""

        with self.transaction() as db:
            self._rebuild_tags(db)

    @staticmethod
    def _fts_expression(text: str) -> str:
        phrase = " ".join((text or "").replace('"', "").split())
        terms = list(dict.fromkeys(TOKEN_RE.findall(phrase)))
        safe = [term.replace('"', "") for term in terms if term]
        expressions: list[str] = []
        if phrase:
            # Keep the user's original contiguous wording intact. SQLite's tokenizer
            # treats a Chinese/Latin run such as "奈奈的酒馆AI" as one token, while a
            # token-only expression would incorrectly require two separate tokens.
            expressions.append(f'"{phrase}"')
        token_expression = " AND ".join(f'"{term}"' for term in safe)
        if token_expression and token_expression not in expressions:
            expressions.append(token_expression)
        return " OR ".join(f"({expression})" for expression in expressions)

    @staticmethod
    def _literal_search_terms(text: str) -> list[str]:
        """Split a whitespace query into independent literal fallback terms."""

        return list(dict.fromkeys(term for term in re.split(r"\s+", (text or "").strip()) if term))

    @staticmethod
    def _advanced_search_expression(query: str) -> tuple[str, list[Any]] | None:
        """Build a literal boolean expression for quoted/AND/OR searches.

        FTS is intentionally bypassed here: its tokenizer can discard symbols
        (for example the stars in ``"⭐⭐⭐⭐⭐ ntl"``), which changes the user's
        requested phrase into a much broader token search.
        """

        raw = query or ""
        if not re.search(r'"[^"\n]*"|&&?|\|\|?|\b(?:AND|OR)\b|[()]', raw, re.IGNORECASE):
            return None
        token_re = re.compile(r'"[^"\n]*"|&&?|\|\|?|\bAND\b|\bOR\b|[()]|[^\s()&|]+', re.IGNORECASE)
        tokens = token_re.findall(raw)
        if not tokens:
            return None
        params: list[Any] = []
        def atom(value: str) -> str:
            value = value[1:-1] if len(value) >= 2 and value[0] == '"' and value[-1] == '"' else value
            value = value.strip()
            if not value:
                return "1=1"
            # A repeated marker such as ``⭐⭐⭐⭐`` must not match the first
            # four characters of ``⭐⭐⭐⭐⭐``. Require the run to be bounded by
            # a different character on both sides.
            repeated_marker = len(value) > 1 and len(set(value)) == 1
            marker_value = value.replace("\ufe0f", "") if repeated_marker else value
            fields = ("m.text", "m.album_text")
            conditions: list[str] = []
            for field in fields:
                expression = f"lower(COALESCE({field}, ''))"
                if repeated_marker:
                    # Telegram text may store emoji with U+FE0F variation
                    # selectors (e.g. ``⭐️``). Compare a normalized form so
                    # plain ``⭐⭐⭐⭐`` cannot match a five-star run.
                    expression = f"replace({expression}, char(65039), '')"
                    conditions.append(
                        f"(instr({expression}, lower(?)) > 0 AND "
                        f"instr({expression}, lower(?)) = 0 AND "
                        f"instr({expression}, lower(?)) = 0)"
                    )
                    params.extend([marker_value, marker_value[0] + marker_value, marker_value + marker_value[0]])
                else:
                    conditions.append(f"instr({expression}, lower(?)) > 0")
                    params.append(marker_value)
            return f"({' OR '.join(conditions)})"

        index = 0

        def primary() -> str:
            nonlocal index
            if index >= len(tokens):
                return "1=1"
            token = tokens[index]
            if token == "(":
                index += 1
                value = expression()
                if index < len(tokens) and tokens[index] == ")":
                    index += 1
                return f"({value})"
            if token in {")", "AND", "OR", "&", "|", "&&", "||"
            }:
                index += 1
                return "1=1"
            index += 1
            return atom(token)

        def conjunction() -> str:
            nonlocal index
            value = primary()
            while index < len(tokens) and tokens[index].upper() in {"AND", "&", "&&"}:
                index += 1
                value = f"({value} AND {primary()})"
            # Whitespace between atoms is also an implicit AND.
            while index < len(tokens) and tokens[index] not in {")", "OR", "|", "||", "AND", "&", "&&"
            }:
                value = f"({value} AND {primary()})"
            return value

        def expression() -> str:
            nonlocal index
            value = conjunction()
            while index < len(tokens) and tokens[index].upper() in {"OR", "|", "||"}:
                index += 1
                value = f"({value} OR {conjunction()})"
            return value

        return expression(), params

    @staticmethod
    def _date_value(value: str | None, end: bool = False) -> str | None:
        if not value:
            return None
        raw = str(value)
        if end and len(raw) == 10:
            try:
                return (date.fromisoformat(raw) + timedelta(days=1)).isoformat()
            except ValueError:
                return raw
        return raw

    def _search_parts(
        self,
        payload: SearchIn,
        *,
        ad_keywords: list[str] | None = None,
        literal_text_fallback: bool = False,
    ) -> tuple[str, list[Any]]:
        where = ["1=1"]
        params: list[Any] = []
        if payload.channel_ids:
            marks = ",".join("?" for _ in payload.channel_ids)
            where.append(f"m.channel_id IN ({marks})")
            params.extend(payload.channel_ids)
        if payload.media_types:
            marks = ",".join("?" for _ in payload.media_types)
            where.append(f"m.media_type IN ({marks})")
            params.extend(payload.media_types)
        from_date = self._date_value(payload.from_date)
        until_date = self._date_value(payload.until_date, end=True)
        if from_date:
            where.append("m.sent_at >= ?")
            params.append(from_date)
        if until_date:
            where.append("m.sent_at < ?")
            params.append(until_date)
        if payload.bookmarked_only:
            where.append("EXISTS (SELECT 1 FROM bookmarks b_filter WHERE b_filter.message_id=m.id)")
        tags = list(dict.fromkeys([normalize_tag(tag) for tag in payload.selected_tags if normalize_tag(tag)] + extract_tags(payload.query)))
        for tag in tags:
            where.append("instr(' ' || m.tags || ' ', ' ' || ? || ' ') > 0")
            params.append(tag)
        text_query = TAG_RE.sub(" ", payload.query or "")
        advanced = self._advanced_search_expression(text_query)
        if advanced is not None:
            expression, expression_params = advanced
            where.append(expression)
            params.extend(expression_params)
            fts = ""
        else:
            fts = self._fts_expression(text_query)
        if fts:
            if literal_text_fallback:
                # FTS tokenizes some CJK runs as one term. Keep contiguous queries
                # literal, but treat whitespace as an AND separator: "音声 舔耳"
                # must also match text where the two terms are not adjacent.
                for term in self._literal_search_terms(text_query):
                    where.append("(instr(m.text, ?) > 0 OR instr(COALESCE(m.album_text, ''), ?) > 0)")
                    params.extend([term, term])
            else:
                where.append("m.id IN (SELECT rowid FROM message_search WHERE message_search MATCH ?)")
                params.append(fts)
        if not payload.include_hidden_ads:
            where.append("(m.ad_fingerprint='' OR NOT EXISTS (SELECT 1 FROM messages newer WHERE newer.channel_id=m.channel_id AND newer.ad_fingerprint=m.ad_fingerprint AND (newer.sent_at>m.sent_at OR (newer.sent_at=m.sent_at AND newer.id>m.id))))")
            for keyword in ad_keywords or []:
                where.append("instr(lower(m.text), lower(?))=0")
                params.append(keyword)
        return " AND ".join(where), params

    def search(self, payload: SearchIn, *, ad_keywords: list[str] | None = None) -> dict[str, Any]:
        where, params = self._search_parts(payload, ad_keywords=ad_keywords)
        album_cte = (
            "WITH album_representatives AS ("
            "SELECT channel_id,grouped_id,MIN(CASE WHEN text<>'' THEN id END) AS text_id,MIN(id) AS first_id "
            "FROM messages WHERE grouped_id IS NOT NULL GROUP BY channel_id,grouped_id"
            ") "
        )
        album_join = " LEFT JOIN album_representatives ar ON ar.channel_id=m.channel_id AND ar.grouped_id=m.grouped_id "
        album_where = " AND (m.grouped_id IS NULL OR m.id=COALESCE(ar.text_id,ar.first_id))"
        with self.transaction() as db:
            total = int(db.execute(
                f"{album_cte}SELECT COUNT(*) FROM messages m{album_join}WHERE {where}{album_where}", params
            ).fetchone()[0])
        text_query = TAG_RE.sub(" ", payload.query or "")
        literal_text_fallback = bool(self._fts_expression(text_query)) and total == 0
        if literal_text_fallback:
            where, params = self._search_parts(
                payload, ad_keywords=ad_keywords, literal_text_fallback=True
            )
            with self.transaction() as db:
                total = int(db.execute(
                    f"{album_cte}SELECT COUNT(*) FROM messages m{album_join}WHERE {where}{album_where}", params
                ).fetchone()[0])
        with self.transaction() as db:
            rows = db.execute(
                album_cte + "SELECT m.*,c.title AS channel_title,c.username AS channel_username,c.kind AS channel_kind,"
                "CASE WHEN b.message_id IS NULL THEN 0 ELSE 1 END AS bookmarked "
                f"FROM messages m JOIN channels c ON c.id=m.channel_id LEFT JOIN bookmarks b ON b.message_id=m.id{album_join}WHERE {where}{album_where} "
                f"ORDER BY m.sent_at {'ASC' if payload.sort_order == 'asc' else 'DESC'},m.id {'ASC' if payload.sort_order == 'asc' else 'DESC'} LIMIT ? OFFSET ?",
                [*params, payload.limit, payload.offset],
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["bookmarked"] = bool(item.get("bookmarked"))
            item["has_link"] = bool(item.get("has_link"))
            item["has_tag"] = bool(item.get("has_tag"))
            item["search_reasons"] = []
            if payload.query:
                item["search_reasons"].append("full-text match")
            if payload.selected_tags or extract_tags(payload.query):
                item["search_reasons"].append("tag filter")
            if not item["search_reasons"]:
                item["search_reasons"].append("filter match")
            items.append(item)
        hidden_total = 0
        if not payload.include_hidden_ads:
            unfiltered = SearchIn.model_validate({**payload.model_dump(), "include_hidden_ads": True})
            visible_where, visible_params = self._search_parts(
                unfiltered, literal_text_fallback=literal_text_fallback
            )
            with self.transaction() as db:
                baseline = int(db.execute(f"{album_cte}SELECT COUNT(*) FROM messages m{album_join}WHERE {visible_where}{album_where}", visible_params).fetchone()[0])
            hidden_total = max(0, baseline - total)
        return {"items": items, "total": total, "hidden_ad_count": hidden_total}

    def message_media(self, message_id: int) -> dict[str, Any]:
        """Return only the remote locator needed for one transient preview."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT id,channel_id,telegram_id,media_type FROM messages WHERE id=?", (message_id,)
            ).fetchone()
        if not row:
            raise KeyError(message_id)
        return dict(row)

    def delete_channel(self, channel_id: int) -> dict[str, Any]:
        with self.transaction() as db:
            channel = db.execute("SELECT id,title FROM channels WHERE id=?", (channel_id,)).fetchone()
            if not channel:
                raise KeyError(channel_id)
            message_count = int(db.execute("SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()[0])
            bookmark_count = int(
                db.execute("SELECT COUNT(*) FROM bookmarks b JOIN messages m ON m.id=b.message_id WHERE m.channel_id=?", (channel_id,)).fetchone()[0]
            )
            db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
            self._rebuild_tags(db)
        return {
            "deleted": True,
            "channel_id": channel_id,
            "title": channel["title"],
            "messages_deleted": message_count,
            "bookmarks_deleted": bookmark_count,
        }

    def set_bookmark(self, message_id: int, bookmarked: bool) -> bool:
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
                raise KeyError(message_id)
            if bookmarked:
                db.execute("INSERT OR IGNORE INTO bookmarks(message_id,created_at) VALUES(?,?)", (message_id, utc_iso()))
            else:
                db.execute("DELETE FROM bookmarks WHERE message_id=?", (message_id,))
        return bookmarked

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> SyncJob:
        def decoded(name: str, default: Any) -> Any:
            try:
                value = json.loads(row[name]) if row[name] else default
            except (TypeError, ValueError, json.JSONDecodeError):
                value = default
            return value

        return SyncJob(
            job_id=row["job_id"], channel_ids=[int(value) for value in decoded("channel_ids", [])],
            limit_per_channel=row["limit_per_channel"], mode=row["sync_mode"] if row["sync_mode"] in {"full", "incremental", "verify"} else "full", state=row["state"], channels_total=row["channels_total"],
            channels_completed=row["channels_completed"], messages_processed=row["messages_processed"],
            current=decoded("current", None), results=decoded("results", []), error=row["error"],
            fingerprint=row["fingerprint"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def save_sync_job(self, job: SyncJob) -> None:
        now = utc_iso()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sync_jobs(job_id,fingerprint,channel_ids,limit_per_channel,sync_mode,state,channels_total,channels_completed,"
                "messages_processed,current,results,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET fingerprint=excluded.fingerprint,channel_ids=excluded.channel_ids,"
                "limit_per_channel=excluded.limit_per_channel,sync_mode=excluded.sync_mode,state=excluded.state,channels_total=excluded.channels_total,"
                "channels_completed=excluded.channels_completed,messages_processed=excluded.messages_processed,current=excluded.current,"
                "results=excluded.results,error=excluded.error,updated_at=excluded.updated_at",
                (
                    job.job_id, job.fingerprint, json.dumps(job.channel_ids), job.limit_per_channel, job.mode, job.state,
                    job.channels_total, job.channels_completed, job.messages_processed,
                    json.dumps(job.current, ensure_ascii=False) if job.current else None,
                    json.dumps(job.results, ensure_ascii=False), job.error, job.created_at or now, now,
                ),
            )

    def load_sync_job(self, job_id: str) -> SyncJob | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job_from_row(row) if row else None

    def delete_sync_job(self, job_id: str) -> bool:
        """Remove a cancelled or completed task without touching archived messages."""

        with self.transaction() as db:
            result = db.execute("DELETE FROM sync_jobs WHERE job_id=?", (job_id,))
            return result.rowcount > 0

    def find_sync_job(self, fingerprint: str) -> SyncJob | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM sync_jobs WHERE fingerprint=? ORDER BY updated_at DESC LIMIT 1", (fingerprint,)
            ).fetchone()
            return self._job_from_row(row) if row else None

    def list_sync_jobs(self, limit: int = 20) -> list[SyncJob]:
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM sync_jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._job_from_row(row) for row in rows]

    def next_queued_sync_job(self) -> SyncJob | None:
        """Return the oldest durable job waiting for the single sync worker."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM sync_jobs WHERE state='queued' ORDER BY created_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
            return self._job_from_row(row) if row else None

    def recover_sync_jobs(self) -> SyncJob | None:
        with self.transaction() as db:
            db.execute(
                "UPDATE sync_jobs SET state='queued',current=NULL,error=NULL,updated_at=? WHERE state='running'",
                (utc_iso(),),
            )
            row = db.execute(
                "SELECT * FROM sync_jobs WHERE state='queued' ORDER BY created_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
            return self._job_from_row(row) if row else None

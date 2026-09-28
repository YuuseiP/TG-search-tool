"""Repair archived Telegram messages whose content arrived as RichMessage."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from telescope_core.gateways import RealTelegramGateway
from telescope_core.storage import Storage


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "data"
FETCH_BATCH_SIZE = 100


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _candidates(store: Storage) -> dict[int, list[int]]:
    with store.transaction() as db:
        rows = db.execute(
            "SELECT channel_id,telegram_id FROM messages "
            "WHERE text='' AND media_type='text' AND album_text='' "
            "ORDER BY channel_id,telegram_id"
        ).fetchall()
    result: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        result[int(row["channel_id"])].append(int(row["telegram_id"]))
    return dict(result)


def _meaningful(message: Any) -> bool:
    return bool(
        getattr(message, "message", None)
        or getattr(message, "media", None)
        or getattr(message, "rich_message", None)
    )


async def repair(data_dir: Path) -> dict[str, int]:
    store = Storage(data_dir)
    candidates = _candidates(store)
    if not candidates:
        return {"candidates": 0, "updated": 0, "rich_messages": 0}

    channels = {
        int(channel["id"]): channel
        for channel in store.indexed_channels(list(candidates))
    }
    updated = 0
    rich_messages = 0
    with tempfile.TemporaryDirectory(prefix="telescope-rich-message-repair-") as directory:
        session_dir = Path(directory)
        shutil.copy2(data_dir / "telegram.session", session_dir / "telegram.session")
        gateway = RealTelegramGateway(session_dir)
        try:
            client = await gateway._connect(store.load_config())
            for channel_id, message_ids in candidates.items():
                channel = channels.get(channel_id)
                if channel is None:
                    continue
                entity = await client.get_entity(channel_id)
                for batch in _chunks(message_ids, FETCH_BATCH_SIZE):
                    response = await client.get_messages(entity, ids=batch)
                    messages = response if isinstance(response, list) else [response]
                    recovered = [message for message in messages if message is not None and _meaningful(message)]
                    if not recovered:
                        continue
                    updated += store.upsert_messages(channel, recovered, refresh_tags=False)["observed"]
                    rich_messages += sum(getattr(message, "rich_message", None) is not None for message in recovered)
            store.refresh_tag_dictionary()
        finally:
            await gateway.close()
    return {
        "candidates": sum(len(ids) for ids in candidates.values()),
        "updated": updated,
        "rich_messages": rich_messages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair empty archived Telegram RichMessage records.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()
    result = asyncio.run(repair(args.data_dir))
    print(
        "Repair completed: "
        f"{result['updated']}/{result['candidates']} candidate records refreshed "
        f"({result['rich_messages']} RichMessage records)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

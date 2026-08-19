"""Deterministic metadata repair.

Why this exists
---------------
Older indexed records were parsed from a degraded source: the Telegram filename
can be missing ("Unknown") or junk (e.g. "video_2024-01-15_23-45-12.mp4") while
the real release name lives in the message caption. The caption was never stored,
so filename-based migrations (v5-v8) could never recover those records and could
even corrupt their titles.

This module repairs metadata from the SOURCE OF TRUTH: the actual storage-channel
messages, read through the authorized user session (USER_SESSION_STRING), the same
mechanism the historical importer already used successfully.

Safety properties:
- Nothing is deleted. Existing documents are updated in place.
- Keyed by (channel_id, message_id) with a unique index -> no duplicates.
- Original filename is preserved; the caption is now stored for future re-parses.
- Targeted mode only touches broken records (Unknown language/title).
- Batched bulk writes, FloodWait-aware, resumable by simply running it again.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from pymongo import UpdateOne
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

log = logging.getLogger("movie-repair")

BATCH_SIZE = 250
FETCH_CHUNK = 100

# Records considered "broken": no usable language and/or a corrupted title.
# These are the records that filename-based migrations could never fix.
BROKEN_QUERY = {
    "enabled": True,
    "$or": [
        {"languages": {"$exists": False}},
        {"languages": []},
        {"languages": "Unknown"},
        {"languages": ["Unknown"]},
        {"title": {"$in": [None, "", "Unknown", "Unknown title"]}},
        {"title_normalized": {"$in": [None, "", "unknown", "unknown title"]}},
    ],
}


async def count_broken(bot) -> int:
    """How many enabled records currently look broken (used by /check)."""
    return await bot.movies.count_documents(BROKEN_QUERY)


def _apply_message(bot, message, batch):
    data = bot.metadata_from_message(message)
    created_at = data.pop("created_at", datetime.now(timezone.utc))
    batch.append(
        UpdateOne(
            {"channel_id": bot.STORAGE_CHANNEL_ID, "message_id": message.id},
            {"$set": data, "$setOnInsert": {"created_at": created_at}},
            upsert=True,
        )
    )


async def repair_from_source(bot, full: bool = False, progress=None):
    """Rebuild metadata for storage-channel posts from the live channel messages.

    bot   : the bot_v2 module (provides movies collection, metadata parser, config)
    full  : True  -> re-read every channel message (slow, complete)
            False -> only re-read messages whose stored record is broken
    progress: async callback(processed, updated) for status updates
    """
    session = os.getenv("USER_SESSION_STRING", "").strip()
    if not session:
        raise RuntimeError("USER_SESSION_STRING is not configured")

    target_ids = None
    if not full:
        cursor = bot.movies.find(BROKEN_QUERY, {"message_id": 1})
        docs = await cursor.to_list(length=500000)
        target_ids = [d["message_id"] for d in docs if d.get("message_id") is not None]
        log.info("Targeted repair: %s broken records to rebuild", len(target_ids))
        if not target_ids:
            return {"mode": "targeted", "targets": 0, "processed": 0, "updated": 0}

    user = TelegramClient(StringSession(session), bot.API_ID, bot.API_HASH)
    processed = 0
    updated = 0
    batch = []
    started = datetime.now(timezone.utc)

    async def flush():
        nonlocal updated, batch
        if batch:
            result = await bot.movies.bulk_write(batch, ordered=False)
            updated += result.modified_count
            batch = []

    async def handle(message):
        nonlocal processed
        if not message or not getattr(message, "media", None) or not getattr(message, "file", None):
            return
        _apply_message(bot, message, batch)
        processed += 1
        if len(batch) >= BATCH_SIZE:
            await flush()
        if progress and (processed == 1 or processed % 250 == 0):
            await progress(processed, updated)

    try:
        await user.start()
        if target_ids is None:
            log.info("Full repair: scanning every storage-channel message...")
            async for message in user.iter_messages(bot.STORAGE_CHANNEL_ID):
                await handle(message)
        else:
            for i in range(0, len(target_ids), FETCH_CHUNK):
                chunk = target_ids[i : i + FETCH_CHUNK]
                while True:
                    try:
                        messages = await user.get_messages(bot.STORAGE_CHANNEL_ID, ids=chunk)
                        break
                    except FloodWaitError as exc:
                        wait = int(exc.seconds) + 5
                        log.warning("Repair flood-wait: sleeping %ss", wait)
                        await asyncio.sleep(wait)
                for message in messages:
                    await handle(message)
                await asyncio.sleep(0.5)  # be gentle with Telegram rate limits
        await flush()
        if progress:
            await progress(processed, updated)
    finally:
        await user.disconnect()

    elapsed = datetime.now(timezone.utc) - started
    log.info("Repair finished: processed=%s updated=%s elapsed=%s", processed, updated, elapsed)
    return {
        "mode": "full" if target_ids is None else "targeted",
        "targets": "all" if target_ids is None else len(target_ids),
        "processed": processed,
        "updated": updated,
    }

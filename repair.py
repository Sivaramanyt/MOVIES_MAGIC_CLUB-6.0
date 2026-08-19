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
import types
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


# ---------------------------------------------------------------------------
# Guarded DB-side reparse
# ---------------------------------------------------------------------------
# The channel re-read above is only needed for records whose source text was
# never stored. Everything else can be re-derived cheaply from the stored
# filename+caption whenever the parser improves (e.g. underscore-separated
# names, bitrate tokens like "640kbps", resolution-first quality labels).
# Guards below make sure a reparse can never DOWNGRADE a record.


def metadata_from_stored(bot, doc):
    """Re-derive metadata from a record's stored source text (filename+caption)."""
    filename = doc.get("filename")
    if filename in (None, "", "Unknown"):
        filename = None
    fake_message = types.SimpleNamespace(
        id=doc.get("message_id"),
        media=True,
        file=types.SimpleNamespace(name=filename),
        raw_text=doc.get("caption") or "",
    )
    return bot.metadata_from_message(fake_message)


def _guarded_update(bot, old, new):
    """Build the $set for a reparse, never making a record worse."""
    update = {}

    new_title = new.get("title") or ""
    old_title = old.get("title") or ""
    title_downgrade = bot.is_junk_title(new_title) and not bot.is_junk_title(old_title)
    if not title_downgrade and (
        new_title != old_title or new.get("title_normalized") != old.get("title_normalized")
    ):
        update["title"] = new_title
        update["title_normalized"] = new.get("title_normalized")

    new_languages = [x for x in (new.get("languages") or []) if x and str(x).lower() != "unknown"]
    old_languages = old.get("languages") or []
    if isinstance(old_languages, str):
        old_languages = [old_languages]
    old_languages_clean = [x for x in old_languages if x and str(x).lower() != "unknown"]
    if new_languages and set(map(str.lower, new_languages)) != set(map(str.lower, old_languages_clean)):
        update["languages"] = new["languages"]
        update["language"] = new.get("language")

    new_quality = new.get("quality") or "Unknown"
    old_quality = old.get("quality") or "Unknown"
    if new_quality != old_quality and new_quality != "Unknown":
        update["quality"] = new_quality

    new_year = new.get("year")
    # skip year when the source was junk — camera filenames carry the UPLOAD date
    # ("video_2024-01-15.mp4"), not the movie year
    if new_year and new_year != old.get("year") and not title_downgrade:
        update["year"] = int(new_year)

    return update


async def reparse_stored_records(bot, progress=None):
    """Re-derive metadata for every enabled record from its stored filename+caption.

    DB-only (no Telegram calls), guarded against downgrades, idempotent: once
    all records match the current parser it updates 0 and becomes a fast no-op.
    """
    cursor = bot.movies.find(
        {"enabled": True},
        {"message_id": 1, "filename": 1, "caption": 1, "title": 1, "title_normalized": 1,
         "year": 1, "language": 1, "languages": 1, "quality": 1},
    )
    scanned = 0
    changed = 0
    updated = 0
    batch = []
    started = datetime.now(timezone.utc)

    async def flush():
        nonlocal updated, batch
        if batch:
            result = await bot.movies.bulk_write(batch, ordered=False)
            updated += result.modified_count
            batch = []

    async for doc in cursor:
        scanned += 1
        try:
            new = metadata_from_stored(bot, doc)
            update = _guarded_update(bot, doc, new)
        except Exception:
            log.exception("Reparse failed for record %s", doc.get("_id"))
            continue
        if update:
            changed += 1
            batch.append(UpdateOne({"_id": doc["_id"]}, {"$set": update}))
        if len(batch) >= BATCH_SIZE:
            await flush()
        if progress and scanned % 500 == 0:
            await progress(scanned, changed)
    await flush()

    elapsed = datetime.now(timezone.utc) - started
    log.info("Reparse finished: scanned=%s changed=%s updated=%s elapsed=%s", scanned, changed, updated, elapsed)
    return {"scanned": scanned, "changed": changed, "updated": updated}

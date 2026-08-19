import logging
import os
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

from bot_v2 import index_message, STORAGE_CHANNEL_ID, API_ID, API_HASH, movies

log = logging.getLogger("movie-importer")


async def import_history(limit=None, progress_callback=None):
    """Import historical media metadata from the configured channel.

    Requires USER_SESSION_STRING for an authorized Telegram user account.
    Media is not downloaded; only metadata and source message IDs are stored.
    """
    session = os.getenv("USER_SESSION_STRING", "").strip()
    if not session:
        raise RuntimeError("USER_SESSION_STRING is not configured")

    user = TelegramClient(StringSession(session), API_ID, API_HASH)
    imported = 0
    scanned = 0
    started = datetime.now(timezone.utc)

    try:
        await user.start()
        async for message in user.iter_messages(STORAGE_CHANNEL_ID, limit=limit):
            scanned += 1
            if message.media and message.file:
                await index_message(message)
                imported += 1

            if progress_callback and (scanned == 1 or scanned % 100 == 0):
                await progress_callback(scanned, imported)

        log.info("Historical import finished: scanned=%s imported=%s elapsed=%s", scanned, imported, datetime.now(timezone.utc) - started)
        return scanned, imported
    finally:
        await user.disconnect()

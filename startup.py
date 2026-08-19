import asyncio
import os
import threading

from telethon.errors import FloodWaitError

from health_server import run_health_server

# Start Koyeb's HTTP health endpoint exactly once, before Telegram/MongoDB work.
threading.Thread(target=run_health_server, daemon=True, name="health-server").start()
print("Health server started early", flush=True)

import bot_v2
import normalization_patch

# The patch provides the verified metadata parser (title/year/languages/quality)
# used for every newly indexed storage-channel post.
normalization_patch.install(bot_v2)

# NOTE: The old blind filename-based auto-migration (v5-v8) is intentionally NOT
# run at startup anymore. It re-parsed the stored "filename" field, which is
# "Unknown"/junk for many old records (captions were never stored), so it could
# never fix them and could even corrupt their titles. Metadata for existing
# records is now rebuilt deterministically from the actual storage-channel
# messages via the admin /repair command (see repair.py).


async def auto_repair_broken_records():
    """One-shot metadata repair for OLD records, run automatically after startup.

    Why: deploying new code never changes existing MongoDB records. Old records
    with Unknown language/title must be rebuilt from the actual storage-channel
    messages (the source of truth) exactly once. After that, broken count stays
    at ~0 and this becomes a fast no-op on every later deploy.
    Safe: targeted (broken records only), in-place, idempotent, non-blocking.
    """
    try:
        from repair import count_broken, repair_from_source
        broken = await count_broken(bot_v2)
        if not broken:
            bot_v2.log.info("Auto-repair: no broken records found; nothing to do.")
            return
        if not os.getenv("USER_SESSION_STRING", "").strip():
            bot_v2.log.warning(
                "Auto-repair: %s broken records need rebuilding, but USER_SESSION_STRING is not set. "
                "Set it on Koyeb, then run /repair.", broken,
            )
            return
        bot_v2.log.info("Auto-repair: rebuilding metadata for %s broken records from the storage channel...", broken)
        async def progress(processed, updated):
            bot_v2.log.info("Auto-repair progress: processed=%s updated=%s", processed, updated)
        stats = await repair_from_source(bot_v2, full=False, progress=progress)
        bot_v2.log.info("Auto-repair complete: %s", stats)
    except Exception:
        bot_v2.log.exception("Auto-repair failed")


async def run_maintenance():
    """Background metadata maintenance, run once after every startup.

    Step 1 repairs broken records from the live channel (stores their captions).
    Step 2 re-derives metadata for ALL records from the stored filename+caption
    with the current parser (guarded — never downgrades a record), so parser
    improvements propagate to old records automatically.
    Both steps are idempotent and become fast no-ops once converged.
    """
    await auto_repair_broken_records()
    try:
        from repair import reparse_stored_records
        bot_v2.log.info("Reparse: re-deriving metadata for all stored records (guarded)...")
        async def progress(scanned, changed):
            bot_v2.log.info("Reparse progress: scanned=%s changed=%s", scanned, changed)
        stats = await reparse_stored_records(bot_v2, progress=progress)
        bot_v2.log.info("Reparse complete: %s", stats)
    except Exception:
        bot_v2.log.exception("Stored-record reparse failed")


async def start_telegram_bot():
    # Do not replace/monkey-patch TelegramClient.start(). Telethon's start()
    # is already awaitable when called from an async event loop. A previous
    # wrapper caused startup/authentication to become unreliable.
    print("Starting Telegram bot...", flush=True)

    while True:
        try:
            await bot_v2.client.start(bot_token=bot_v2.BOT_TOKEN)
            break
        except FloodWaitError as exc:
            wait_seconds = int(exc.seconds) + 5
            print(
                f"Telegram authorization is rate-limited; waiting {wait_seconds} seconds before retrying.",
                flush=True,
            )
            await asyncio.sleep(wait_seconds)

    me = await bot_v2.client.get_me()
    bot_v2.log.info("Bot started as @%s", me.username)
    await bot_v2.ensure_indexes()
    bot_v2.log.info("Automatic channel indexing is enabled. Waiting for new storage-channel posts...")
    bot_v2.log.info("Metadata repair is available via the admin /repair command (no auto-migration runs at startup).")

    # Rebuild/re-derive old record metadata automatically, in the background.
    asyncio.create_task(run_maintenance())

    await bot_v2.client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(start_telegram_bot())

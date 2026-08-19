import asyncio
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

    await bot_v2.client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(start_telegram_bot())

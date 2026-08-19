import asyncio
import threading

from telethon.errors import FloodWaitError

from health_server import run_health_server

# Start Koyeb's HTTP health endpoint exactly once, before Telegram/MongoDB work.
threading.Thread(target=run_health_server, daemon=True, name="health-server").start()
print("Health server started early", flush=True)

import bot_v2
import normalization_patch

normalization_patch.install(bot_v2)


async def background_normalization():
    print("Starting background movie title normalization...", flush=True)
    try:
        updated = await normalization_patch.migrate_existing_movies(bot_v2)
        print(
            f"Movie title normalization complete; updated {updated} existing records.",
            flush=True,
        )
    except Exception:
        bot_v2.log.exception("Background movie title normalization failed")


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

    # MongoDB cleanup must never block Telegram updates.
    asyncio.create_task(background_normalization())

    await bot_v2.client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(start_telegram_bot())

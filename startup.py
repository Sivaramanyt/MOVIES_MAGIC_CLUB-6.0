import asyncio
import threading

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from health_server import run_health_server

# Koyeb health endpoint must be available immediately.
threading.Thread(target=run_health_server, daemon=True).start()
print("Health server started early", flush=True)

_original_start = TelegramClient.start


async def safe_start(self, *args, **kwargs):
    while True:
        try:
            return await _original_start(self, *args, **kwargs)
        except FloodWaitError as exc:
            wait_seconds = int(exc.seconds) + 5
            print(
                f"Telegram authorization is rate-limited; waiting {wait_seconds} seconds before retrying.",
                flush=True,
            )
            await asyncio.sleep(wait_seconds)


TelegramClient.start = safe_start

import bot_v2
import normalization_patch

normalization_patch.install(bot_v2)


async def background_normalization():
    try:
        updated = await normalization_patch.migrate_existing_movies(bot_v2)
        print(
            f"Movie title normalization complete; updated {updated} existing records.",
            flush=True,
        )
    except Exception:
        # Normalization must never prevent the Telegram bot from starting.
        bot_v2.log.exception("Background movie title normalization failed")


async def main():
    # Start the bot first. The one-time MongoDB migration runs in the background
    # so a large collection cannot make Telegram appear offline.
    asyncio.create_task(background_normalization())
    print("Starting Telegram bot...", flush=True)
    await bot_v2.main()


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import threading

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from health_server import run_health_server

# Koyeb health endpoint must be available immediately. bot_v2 must not start
# another copy of the health server.
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
    print("Starting background movie title normalization...", flush=True)
    try:
        updated = await normalization_patch.migrate_existing_movies(bot_v2)
        print(
            f"Movie title normalization complete; updated {updated} existing records.",
            flush=True,
        )
    except Exception:
        # Never take the Telegram bot offline because of a migration problem.
        bot_v2.log.exception("Background movie title normalization failed")


async def main():
    # Telegram starts first. MongoDB cleanup runs independently so a large
    # collection can never prevent the bot from responding.
    print("Starting Telegram bot...", flush=True)
    bot_task = asyncio.create_task(bot_v2.main())
    await asyncio.sleep(0.1)
    asyncio.create_task(background_normalization())
    await bot_task


if __name__ == "__main__":
    asyncio.run(main())

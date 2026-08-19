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


async def normalize_before_bot():
    print("Starting movie title normalization...", flush=True)
    try:
        updated = await normalization_patch.migrate_existing_movies(bot_v2)
        print(
            f"Movie title normalization complete; updated {updated} existing records.",
            flush=True,
        )
    except Exception:
        bot_v2.log.exception("Movie title normalization failed")
        raise


async def main():
    # The health endpoint is already live, so Koyeb remains healthy while the
    # one-time MongoDB cleanup runs. Starting Telegram after the cleanup makes
    # the search UI deterministic: old release filenames cannot leak into the
    # first search result.
    await normalize_before_bot()
    print("Starting Telegram bot...", flush=True)
    await bot_v2.main()


if __name__ == "__main__":
    asyncio.run(main())

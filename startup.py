import asyncio

from telethon import TelegramClient
from telethon.errors import FloodWaitError

_original_start = TelegramClient.start


async def safe_start(self, *args, **kwargs):
    while True:
        try:
            return await _original_start(self, *args, **kwargs)
        except FloodWaitError as exc:
            wait_seconds = int(exc.seconds) + 5
            print(f"Telegram authorization is rate-limited; waiting {wait_seconds} seconds before retrying.", flush=True)
            await asyncio.sleep(wait_seconds)


TelegramClient.start = safe_start

import bot_v2


if __name__ == "__main__":
    asyncio.run(bot_v2.main())

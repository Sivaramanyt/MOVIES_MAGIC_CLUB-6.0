# Movie Magic Club 6.0

A Telegram **media filter bot** built with Telethon and MongoDB.

> Use this bot only with movies/media that you own, have permission to distribute, or are otherwise legally allowed to share.

## Flow

1. User sends a movie name, for example `Leo`.
2. Bot searches the MongoDB catalog.
3. If multiple movies match, the bot shows the movie/year choices.
4. User selects a movie.
5. Bot shows available languages.
6. User selects a language.
7. Bot shows available qualities.
8. User selects a quality.
9. Bot sends/copies the matching Telegram media from the configured storage channel to the user's private chat.

## Stack

- Python 3.10+
- Telethon (not Pyrogram)
- MongoDB / Motor
- Telegram channel as media storage

## Environment variables

Copy `.env.example` to `.env` and fill in:

- `API_ID` / `API_HASH`: Telegram API credentials from my.telegram.org
- `BOT_TOKEN`: BotFather token
- `MONGO_URI`: MongoDB connection string
- `MONGO_DB`: database name
- `STORAGE_CHANNEL_ID`: channel ID where authorized media is stored
- `ADMIN_IDS`: comma-separated Telegram user IDs allowed to run admin commands

## Cataloging media

The bot automatically indexes **new posts** arriving in the configured storage channel when the post contains a supported media file and a parseable filename/caption.

For reliable metadata, use a caption such as:

```text
Leo
Year: 2023
Language: Tamil
Quality: 1080p
```

or a filename such as:

```text
Leo (2023) - Tamil - 1080p.mkv
```

Admins can also use `/add` to add an existing channel message manually:

```text
/add <message_id> | Leo | 2023 | Tamil | 1080p
```

The message must already exist in the configured storage channel.

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

The bot uses inline buttons and callback queries, so users do not need to type each selection manually.


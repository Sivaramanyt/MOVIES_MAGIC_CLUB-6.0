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

## Admin commands

- `/check <movie name>` — show the **actual stored MongoDB records** for a movie
  (title, year, languages, quality, filename, whether a caption is stored).
  This is the source-of-truth diagnostic for metadata problems.
- `/repair` — rebuild metadata for **broken records** (Unknown language/title)
  by re-reading the actual storage-channel messages through the user session.
  Nothing is deleted; records are updated in place, keyed by
  `(channel_id, message_id)` — no duplicates.
- `/repair all` — same, but re-reads **every** storage-channel message.
- `/stats` — indexed files, unique movies, language/quality distribution,
  and the number of broken records.

`ADMIN_IDS` must contain your Telegram user ID for these to work, and
`USER_SESSION_STRING` (an authorized Telegram user session) must be set for
`/repair`, exactly like the historical importer.

## Cataloging media

The bot automatically indexes **new posts** arriving in the configured storage channel when the post contains a supported media file and a parseable filename/caption. Metadata is mined from **both** the filename and the caption: languages are unioned (so `[Malayalam + Kannada]` files appear under both languages), and the caption is stored so metadata can always be re-parsed later.

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

## Shortlink verification (monetization)

Free tier: every user gets **3 free file deliveries per day** (resets at midnight IST).
The 4th delivery shows a verification gate instead: the user completes one
shortlink and gets **unlimited files for 24 hours**. When the window expires,
the user returns to the free tier with a fresh 3-file allowance.

Flow:

1. User hits the daily limit → bot shows a **Verify & Get Unlimited** button.
2. The button is your monetized shortlink wrapping
   `BASE_URL/verify/<one-time-token>`.
3. The user finishes the shortlink → lands on `/verify/<token>` (served by the
   built-in HTTP server alongside the Koyeb health endpoint) → success page.
4. Back in Telegram the user taps the quality again → files are delivered.

Setup (env vars):

- `SHORTLINK_API` / `SHORTLINK_URL` — your shortlink service credentials
  (arolinks/gplinks/shrinkme style `GET <service>/api?api=KEY&url=...`; several
  response formats are tried automatically). If unset, the raw verification
  link is used (no monetization).
- `BASE_URL` — public URL of the app. On Koyeb this falls back to the
  automatic `KOYEB_PUBLIC_DOMAIN`, so usually nothing to set.
- `FREE_LIMIT` (default 3), `VERIFY_VALID_HOURS` (default 24),
  `VERIFICATION_ENABLED` (default true), `BOT_USERNAME`.

Admin commands (runtime, stored in Mongo — no redeploy needed):

- `/verifyon` / `/verifyoff` — enable/disable the gate
- `/verifylimit N` — free files per day per user
- `/verifyhours N` — unlimited window length after verifying
- `/verifystatus` — current settings, tracked users, verified users

Admins (ADMIN_IDS) always bypass the gate. Verification state lives in the
`verifications` and `verify_tokens` MongoDB collections; tokens are one-time
and expire after `VERIFY_TOKEN_HOURS` (default 24).

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

The bot uses inline buttons and callback queries, so users do not need to type each selection manually.


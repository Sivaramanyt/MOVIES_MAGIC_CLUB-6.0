"""File-to-link: fast download + watch online links attached to delivered files.

When the bot delivers a file, a random link token is minted (stored in Mongo,
default 48h TTL) and two links are attached to the file's caption + buttons:

    ➙ Download      {BASE_URL}/dl/<token>     (attachment — IDM/1DM friendly)
    ➙ Watch Online  {BASE_URL}/watch/<token>  (HTML5 player page)

This module also owns the public aiohttp web server that runs on the bot's
event loop (replacing the old threaded health server in production):

    GET /health            Koyeb health checks (also "/" and "/healthz")
    GET /verify/<token>    shortlink verification callback (same behavior as before)
    GET /dl/<token>        stream the file as an attachment (HTTP Range supported)
    GET /stream/<token>    same stream, inline — used by the watch page player
    GET /watch/<token>     HTML5 player page

Streaming pulls the file from Telegram on demand in 256KB chunks with full
HTTP Range support, which is what makes IDM/1DM multi-connection downloads
and video-player seeking work.

Env vars:
    PORT              (default 8000)
    BASE_URL          public app URL; falls back to KOYEB_PUBLIC_DOMAIN
    LINK_TTL_HOURS    download/watch link lifetime (default 48)
    BOT_USERNAME      used for the "CC : @..." caption line and page buttons
"""

import asyncio
import logging
import mimetypes
import os
import secrets
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

from aiohttp import web
from telethon import Button

from verification import public_base_url, utcnow

log = logging.getLogger("movie-links")

PORT = int(os.getenv("PORT", "8000"))
LINK_TTL = timedelta(hours=int(os.getenv("LINK_TTL_HOURS", "48")))
BOT_USERNAME = os.getenv("BOT_USERNAME", "MOVIES_MAGIC_CLUB_bot").lstrip("@")
VERIFY_VALID_HOURS_DEFAULT = int(os.getenv("VERIFY_VALID_HOURS", "24"))
TOKEN_TTL_HOURS = int(os.getenv("VERIFY_TOKEN_HOURS", "24"))
CHUNK_SIZE = 256 * 1024  # 256KB per Telegram chunk (IDM-friendly)


# ---------------------------------------------------------------- caption ----
def human_size(num):
    """557.31 MB style (2-decimal units, matches the example format)."""
    n = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def build_file_caption(filename, size_bytes, dl_url, watch_url, bot_username=BOT_USERNAME):
    """Caption attached to every delivered file — the file-to-link format."""
    return (
        f"‣ File Name : {filename}\n\n"
        f"‣ File Size : {human_size(size_bytes)}\n\n"
        f"➙ Download : {dl_url}\n\n"
        f"➙ Watch Online : {watch_url}\n\n"
        "💡 Tip :- Use IDM (For PC) or 1DM (For Mobile) To Download With Maximum Speed\n\n"
        f"CC : @{bot_username}"
    )


def build_plain_caption(filename, size_bytes, bot_username=BOT_USERNAME):
    """Fallback caption when no public BASE_URL is configured (no links possible)."""
    return (
        f"‣ File Name : {filename}\n\n"
        f"‣ File Size : {human_size(size_bytes)}\n\n"
        f"CC : @{bot_username}"
    )


# ---------------------------------------------------------------- tokens ----
async def mint_link_token(db, msg, doc):
    """Create one download/watch token for a delivered file. Returns the token."""
    token = secrets.token_hex(12)  # 24 hex chars, e.g. 6a7bb27c913c8281a20eca89
    now = utcnow()
    filename = (msg.file.name if msg.file else None) or doc.get("filename") or "file"
    await db.file_links.insert_one({
        "token": token,
        "channel_id": doc.get("channel_id"),
        "message_id": doc.get("message_id"),
        "filename": filename,
        "size": (msg.file.size if msg.file else None) or 0,
        "mime": (msg.file.mime_type if msg.file else None) or "",
        "created_at": now,
        "expires_at": now + LINK_TTL,
    })
    return token


async def ensure_link_indexes(db):
    """Unique token index + TTL auto-cleanup of expired link records."""
    try:
        await db.file_links.create_index("token", unique=True)
        await db.file_links.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        log.exception("Could not create file_links indexes (non-fatal)")


async def deliver_file(bot, user_id, doc):
    """Send one stored channel file to a user with fast download + watch links.

    Replaces a plain forward: the file is re-sent by reference (no re-upload)
    with the file-to-link caption and Download/Watch Online URL buttons.
    Raises ValueError when the stored channel message is gone.
    """
    channel_id = doc.get("channel_id") or bot.STORAGE_CHANNEL_ID
    msg = await bot.client.get_messages(channel_id, ids=doc["message_id"])
    if not msg or not getattr(msg, "media", None) or not getattr(msg, "file", None):
        raise ValueError(f"stored channel message {doc.get('message_id')} is unavailable")

    filename = msg.file.name or doc.get("filename") or "file"
    size = msg.file.size or 0
    base = public_base_url()
    buttons = None
    if base:
        token = await mint_link_token(bot.db, msg, doc)
        dl_url = f"{base}/dl/{token}"
        watch_url = f"{base}/watch/{token}"
        caption = build_file_caption(filename, size, dl_url, watch_url)
        buttons = [[Button.url("➙ Download", dl_url), Button.url("▶️ Watch Online", watch_url)]]
    else:
        log.warning(
            "BASE_URL/KOYEB_PUBLIC_DOMAIN is not set — delivering %s without download/watch links",
            filename,
        )
        caption = build_plain_caption(filename, size)
    # parse_mode=None: filenames contain _ * [ ] etc. — never let markdown mangle them.
    await bot.client.send_file(user_id, msg.media, caption=caption, buttons=buttons, parse_mode=None)


# ---------------------------------------------------------------- pages ----
def page(title, heading, body_html, show_bot_button=False):
    button = ""
    if show_bot_button:
        button = (
            f'<a class="btn" href="https://t.me/{escape(BOT_USERNAME)}?start=verified">'
            f"⬅️ Back to @{escape(BOT_USERNAME)}</a>"
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:16px;box-sizing:border-box}}
.card{{background:#1e293b;padding:32px 28px;border-radius:16px;max-width:430px;text-align:center;box-shadow:0 10px 30px rgba(0,0,0,.45)}}
h1{{font-size:24px;margin:0 0 14px}}
p{{color:#94a3b8;line-height:1.55;margin:0}}
b{{color:#e2e8f0}}
.btn{{display:inline-block;margin-top:22px;background:#22c55e;color:#052e16;padding:13px 26px;border-radius:10px;text-decoration:none;font-weight:700}}
</style></head>
<body><div class="card"><h1>{heading}</h1><p>{body_html}</p>{button}</div></body></html>"""


def error_page(title, heading, body_html):
    return page(title, heading, body_html + "<br><br>Go back to the bot and request the file again to get a fresh link.", show_bot_button=True)


def watch_page(token, filename, size, mime):
    src = f"/stream/{token}"
    dl = f"/dl/{token}"
    is_video = (mime or "").startswith("video/") or filename.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".avi"))
    player = (
        f'<video controls preload="metadata" src="{src}">Your browser does not support the video tag.</video>'
        if is_video else
        "<p>🎧 Preview is available for video files only — use the download button below.</p>"
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>▶️ {escape(filename)}</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px;box-sizing:border-box;display:flex;justify-content:center}}
.wrap{{width:100%;max-width:900px}}
h1{{font-size:18px;word-break:break-all;margin:8px 0 14px}}
video{{width:100%;max-height:70vh;background:#000;border-radius:12px}}
.meta{{color:#94a3b8;margin:14px 0 0}}
.btn{{display:inline-block;margin-top:16px;background:#22c55e;color:#052e16;padding:13px 26px;border-radius:10px;text-decoration:none;font-weight:700}}
.tip{{color:#94a3b8;margin-top:18px;font-size:14px}}
</style></head>
<body><div class="wrap">
<h1>▶️ {escape(filename)}</h1>
{player}
<p class="meta">‣ File Size : {escape(human_size(size))}</p>
<a class="btn" href="{dl}">⬇️ Fast Download</a>
<p class="tip">💡 Tip :- Use IDM (For PC) or 1DM (For Mobile) To Download With Maximum Speed</p>
</div></body></html>"""


# ---------------------------------------------------------------- verify ----
async def process_verify_token(db, token, now=None):
    """Consume a one-time verification token and grant unlimited access.

    Same behavior as the old threaded health server, but async (motor) so it
    runs on the bot's event loop. Returns (ok, detail): ok=True → detail is
    the verified_until datetime; ok=False → human-readable failure reason.
    """
    now = now or utcnow()
    token_doc = await db.verify_tokens.find_one_and_update(
        {"token": token, "used": {"$ne": True}},
        {"$set": {"used": True, "used_at": now}},
    )
    if not token_doc:
        return False, "This verification link is invalid or was already used."
    created = token_doc.get("created_at")
    if created and (now - created) > timedelta(hours=TOKEN_TTL_HOURS):
        return False, "This verification link has expired."
    settings = await db.settings.find_one({"_id": "verification"}) or {}
    hours = int(settings.get("valid_hours", VERIFY_VALID_HOURS_DEFAULT) or VERIFY_VALID_HOURS_DEFAULT)
    verified_until = now + timedelta(hours=hours)
    await db.verifications.update_one(
        {"_id": token_doc["user_id"]},
        {
            "$set": {"verified_until": verified_until, "free_used": 0, "updated_at": now},
            "$setOnInsert": {"day": ""},
        },
        upsert=True,
    )
    return True, verified_until


# ---------------------------------------------------------------- handlers ----
async def health(request):
    return web.Response(text="OK", content_type="text/plain")


async def verify_handler(request):
    db = request.app["bot"].db
    token = request.match_info["token"]
    try:
        ok, detail = await process_verify_token(db, token)
    except Exception:
        log.exception("Verification callback failed")
        return web.Response(
            status=500,
            text=page("Error", "⚠️ Error", "Something went wrong. Please try again."),
            content_type="text/html",
        )
    if ok:
        until_ist = detail + timedelta(hours=5, minutes=30)
        html = page(
            "Verification successful",
            "✅ Verification successful!",
            "You now have <b>UNLIMITED movies for 24 hours</b>. 🎉<br><br>"
            f"Valid until <b>{until_ist:%d %b %Y, %I:%M %p} IST</b>.<br>"
            "Return to Telegram and choose your movie again.",
            show_bot_button=True,
        )
    else:
        html = page(
            "Verification failed",
            "❌ Link not valid",
            escape(detail) + "<br><br>Go back to the bot and tap the verification button again to get a fresh link.",
            show_bot_button=True,
        )
    return web.Response(status=200, text=html, content_type="text/html")


def parse_range_header(value, size):
    """Parse a Range header against the file size.

    Returns (start, end) inclusive. Raises ValueError for an unsatisfiable
    range (caller answers 416). Only the first range of a multi-range request
    is honored — IDM/1DM request one range per connection anyway.
    """
    if not value:
        raise ValueError("empty range")
    unit, _, spec = value.partition("=")
    if unit.strip().lower() != "bytes" or not spec:
        raise ValueError("unsupported range unit")
    first = spec.split(",", 1)[0].strip()
    start_s, dash, end_s = first.partition("-")
    if not dash:
        raise ValueError("malformed range")
    if start_s == "":
        # suffix range: last N bytes
        suffix = int(end_s)
        if suffix <= 0:
            raise ValueError("empty suffix range")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError("range not satisfiable")
    return start, min(end, size - 1)


def _content_disposition(disposition_type, filename):
    safe = filename.replace('"', "").replace("\r", " ").replace("\n", " ")
    return f"{disposition_type}; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


async def _lookup_link(request):
    """Return (link_doc, error_response). error_response is None on success."""
    db = request.app["bot"].db
    token = request.match_info["token"]
    link = await db.file_links.find_one({"token": token})
    if not link:
        return None, web.Response(
            status=404,
            text=error_page("Link not found", "❌ Link not valid", "This download link is invalid or was already removed."),
            content_type="text/html",
        )
    expires_at = link.get("expires_at")
    if expires_at and expires_at <= utcnow():
        return None, web.Response(
            status=410,
            text=error_page("Link expired", "⏰ Link expired", "Download links stay active for 48 hours. This one has expired."),
            content_type="text/html",
        )
    return link, None


async def _stream_file(request, disposition_type):
    """Shared core for /dl (attachment) and /stream (inline for the player)."""
    bot = request.app["bot"]
    link, err = await _lookup_link(request)
    if err:
        return err
    try:
        msg = await bot.client.get_messages(link["channel_id"], ids=link["message_id"])
    except Exception:
        log.exception("Could not fetch channel message %s for a link token", link.get("message_id"))
        msg = None
    if not msg or not getattr(msg, "media", None) or not getattr(msg, "file", None):
        return web.Response(
            status=404,
            text=error_page("File unavailable", "⚠️ File unavailable", "The stored file is no longer available on Telegram."),
            content_type="text/html",
        )

    size = msg.file.size or link.get("size") or 0
    filename = msg.file.name or link.get("filename") or "file"
    mime = (
        msg.file.mime_type
        or link.get("mime")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    start, end = 0, max(size - 1, 0)
    status = 200
    headers = {"Accept-Ranges": "bytes"}
    range_header = request.headers.get("Range")
    if range_header and size > 0:
        try:
            start, end = parse_range_header(range_header, size)
            status = 206
        except ValueError:
            return web.Response(
                status=416,
                headers={"Content-Range": f"bytes */{size}"},
                text="Requested range not satisfiable",
            )
    length = end - start + 1
    headers["Content-Type"] = mime
    headers["Content-Length"] = str(length)
    headers["Content-Disposition"] = _content_disposition(disposition_type, filename)
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)
    try:
        # NB: telethon's iter_download `limit` counts CHUNKS, not bytes — so
        # the exact byte range is enforced here by counting bytes ourselves.
        remaining = length
        async for chunk in bot.client.iter_download(msg.media, offset=start, chunk_size=CHUNK_SIZE):
            if len(chunk) > remaining:
                await resp.write(bytes(chunk[:remaining]))
                remaining = 0
            else:
                await resp.write(chunk)
                remaining -= len(chunk)
            if remaining <= 0:
                break
    except (ConnectionError, asyncio.CancelledError):
        log.info("Client disconnected while streaming %s", filename)
    except Exception:
        log.exception("Telegram stream failed for %s", filename)
    try:
        await resp.write_eof()
    except Exception:
        pass
    return resp


async def download_handler(request):
    return await _stream_file(request, "attachment")


async def stream_handler(request):
    return await _stream_file(request, "inline")


async def watch_handler(request):
    link, err = await _lookup_link(request)
    if err:
        return err
    html = watch_page(
        token=request.match_info["token"],
        filename=link.get("filename") or "file",
        size=link.get("size") or 0,
        mime=link.get("mime") or "",
    )
    return web.Response(status=200, text=html, content_type="text/html")


# ---------------------------------------------------------------- server ----
def create_app(bot):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/verify/{token}", verify_handler)
    app.router.add_get("/dl/{token}", download_handler)
    app.router.add_get("/stream/{token}", stream_handler)
    app.router.add_get("/watch/{token}", watch_handler)
    return app


async def start_web_server(bot):
    """Start the public web server on the bot's event loop (PORT, default 8000).

    Must be awaited BEFORE connecting to Telegram so Koyeb health checks pass
    immediately and stay up during Telegram reconnects.
    """
    await ensure_link_indexes(bot.db)
    runner = web.AppRunner(create_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(
        "Web server started on port %s — /health, /verify/<token>, /dl/<token>, /watch/<token> (links valid %sh)",
        PORT, int(LINK_TTL.total_seconds() // 3600),
    )
    return runner

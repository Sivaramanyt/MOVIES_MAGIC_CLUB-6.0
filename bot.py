import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from threading import Thread

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageIdInvalidError

from health_server import run_health_server

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("movie-bot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.getenv("MONGO_DB", "movie_magic_club")
STORAGE_CHANNEL_ID = int(os.environ["STORAGE_CHANNEL_ID"])
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

client = TelegramClient("movie_magic_club_bot", API_ID, API_HASH)
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[MONGO_DB]
movies = db.movies
SEARCH_LIMIT = 200
LANGUAGES = ("Tamil", "Telugu", "Malayalam", "Kannada", "Hindi", "English", "Bengali", "Marathi")
import_lock = asyncio.Lock()

NOISE_WORDS = (
    "tamilmv", "tamilblasters", "1tamilblasters", "tamilrockers", "isaimini", "hdt", "nf", "webdl", "web", "dl",
    "webrip", "bluray", "brrip", "hdrip", "dvdrip", "dvdscr", "hdcam", "cam", "proper", "hq", "true", "untouched",
    "remux", "x264", "x265", "hevc", "avc", "aac", "dd", "dd5", "ddp", "ddp5", "atmos", "mkv", "mp4", "avi",
    "mov", "dual", "audio", "multi", "sample", "repack", "rip", "subs", "subtitle", "re"
)

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", value)
    return clean_text(value)

def canonical_movie_title(value: str) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"@[A-Za-z0-9_]+", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b(?:2160p|1080p|720p|480p|360p|4k)\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:gb|gib|mb|mib)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:" + "|".join(re.escape(x) for x in NOISE_WORDS) + r")\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"[._+\-]+", " ", text)
    text = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", text)
    return clean_text(text)

def display_title(value: str) -> str:
    title = canonical_movie_title(value)
    return title.title() if title else clean_text(value).title()

def parse_metadata(text: str):
    text = clean_text(text)
    year_match = re.search(r"(?:19|20)\d{2}", text)
    year = int(year_match.group()) if year_match else None
    language = next((x for x in LANGUAGES if re.search(rf"\b{re.escape(x)}\b", text, re.I)), None)
    quality_match = re.search(r"\b(2160p|4K|1080p|720p|480p|360p)\b", text, re.I)
    quality = quality_match.group(1) if quality_match else None
    title = re.sub(r"[._-]+", " ", text)
    title = re.sub(r"\b(2160p|4K|1080p|720p|480p|360p)\b", " ", title, flags=re.I)
    if year:
        title = re.sub(rf"\b{year}\b", " ", title)
    for lang in LANGUAGES:
        title = re.sub(rf"\b{re.escape(lang)}\b", " ", title, flags=re.I)
    return clean_text(title) or text, year, language, quality

def metadata_from_message(message):
    caption = message.raw_text or ""
    filename = message.file.name if message.file else None
    source = filename or caption or "Unknown"
    title, year, language, quality = parse_metadata(source)
    overrides = {
        "year": r"(?:year)\s*[:=-]\s*((?:19|20)\d{2})",
        "language": r"(?:language|lang)\s*[:=-]\s*([^\n|]+)",
        "quality": r"(?:quality)\s*[:=-]\s*([^\n|]+)",
    }
    for key, pattern in overrides.items():
        match = re.search(pattern, caption, re.I)
        if match:
            value = clean_text(match.group(1))
            if key == "year": year = int(value)
            elif key == "language": language = value
            else: quality = value
    return {
        "title": title,
        "title_normalized": normalize_title(title),
        "year": year,
        "language": language or "Unknown",
        "quality": quality or "Unknown",
        "channel_id": STORAGE_CHANNEL_ID,
        "message_id": message.id,
        "filename": filename or "Unknown",
        "created_at": datetime.now(timezone.utc),
        "enabled": True,
    }

async def index_message(message):
    if not message.media or not message.file:
        return False
    data = metadata_from_message(message)
    await movies.update_one({"channel_id": STORAGE_CHANNEL_ID, "message_id": message.id}, {"$set": data}, upsert=True)
    log.info("Indexed message %s: %s | %s | %s | %s", message.id, data["title"], data["year"], data["language"], data["quality"])
    return True

async def remove_message(message_id: int):
    await movies.update_one({"channel_id": STORAGE_CHANNEL_ID, "message_id": message_id}, {"$set": {"enabled": False, "deleted_at": datetime.now(timezone.utc)}})

@client.on(events.NewMessage(chats=STORAGE_CHANNEL_ID))
async def storage_channel_new_message(event):
    try: await index_message(event.message)
    except Exception: log.exception("Automatic indexing failed for message %s", event.message.id)

@client.on(events.MessageEdited(chats=STORAGE_CHANNEL_ID))
async def storage_channel_edited_message(event):
    try:
        if event.message.media and event.message.file: await index_message(event.message)
        else: await remove_message(event.message.id)
    except Exception: log.exception("Automatic re-indexing failed for message %s", event.message.id)

@client.on(events.MessageDeleted(chats=STORAGE_CHANNEL_ID))
async def storage_channel_deleted_message(event):
    try:
        for message_id in event.deleted_ids: await remove_message(message_id)
    except Exception: log.exception("Automatic delete synchronization failed")

async def search_movies(query: str):
    normalized = normalize_title(query)
    if not normalized: return []
    words = normalized.split()
    pattern = ".*" + ".*".join(re.escape(w) for w in words) + ".*"
    docs = await movies.find({"enabled": True, "title_normalized": {"$regex": pattern}}, {"title": 1, "year": 1, "language": 1, "quality": 1, "message_id": 1, "filename": 1}).sort([("year", -1), ("title", 1)]).limit(SEARCH_LIMIT).to_list(length=SEARCH_LIMIT)
    groups = {}
    for doc in docs:
        title_key = canonical_movie_title(doc.get("title") or doc.get("filename") or "")
        if not title_key: continue
        group_key = (title_key, doc.get("year"))
        if group_key not in groups:
            groups[group_key] = {"_id": doc["_id"], "title": display_title(title_key), "year": doc.get("year"), "file_count": 0}
        groups[group_key]["file_count"] += 1
    return sorted(groups.values(), key=lambda x: (x.get("year") is None, -(x.get("year") or 0), x["title"].lower()))

def result_buttons(items):
    return [[Button.inline(f"🎬 {item['title']} ({item.get('year') or 'Year unknown'}) • {item['file_count']} files"[:64], data=f"movie:{item['_id']}")] for item in items]

async def group_files(base):
    """Find files by logical title/year. This deliberately does NOT use the raw title as the group key."""
    key = canonical_movie_title(base.get("title") or base.get("filename") or "")
    query = {"enabled": True, "year": base.get("year")} if base.get("year") is not None else {"enabled": True}
    candidates = await movies.find(query, {"title": 1, "filename": 1, "year": 1, "language": 1, "quality": 1, "message_id": 1}).limit(SEARCH_LIMIT * 5).to_list(length=SEARCH_LIMIT * 5)
    return [d for d in candidates if canonical_movie_title(d.get("title") or d.get("filename") or "") == key]

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    await event.respond("🎬 **Movie Magic Club**\n\nSend a movie name here or in the movie group. Choose movie/year → language → quality, and I will send the selected file to your private chat.")

@client.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel(event): await event.respond("❌ Search cancelled. Send a movie name to search again.")

@client.on(events.NewMessage(pattern=r"^/import(?:\s+(\d+))?$"))
async def import_command(event):
    if event.sender_id not in ADMIN_IDS: return
    if import_lock.locked(): await event.respond("⏳ An import is already running."); return
    limit_text = event.pattern_match.group(1); limit = int(limit_text) if limit_text else None
    await event.respond(f"📥 Starting historical channel import{f' (max {limit} messages)' if limit else ''}...\n\nThe bot will remain online while the importer reads the channel history.")
    async def progress(scanned, imported):
        if scanned % 500 == 0:
            try: await event.respond(f"📊 Import progress: scanned **{scanned}**, indexed **{imported}** media files.")
            except Exception: pass
    async with import_lock:
        try:
            from historical_import import import_history
            scanned, imported = await import_history(limit=limit, progress_callback=progress)
            await event.respond(f"✅ Import complete.\n\n📦 Scanned: **{scanned}**\n🎬 Indexed: **{imported}**")
        except Exception as exc:
            log.exception("Historical import failed"); await event.respond(f"❌ Import failed: `{type(exc).__name__}: {exc}`")

@client.on(events.NewMessage(pattern=r"^/add\s+(.+)$"))
async def add_existing(event):
    if event.sender_id not in ADMIN_IDS: return
    parts = [clean_text(p) for p in event.pattern_match.group(1).split("|")]
    if len(parts) != 5:
        await event.respond("Usage: `/add <message_id> | title | year | language | quality`"); return
    message_id, title, year, language, quality = parts
    try:
        message_id = int(message_id); year = int(year); message = await client.get_messages(STORAGE_CHANNEL_ID, ids=message_id)
    except (ValueError, TypeError): await event.respond("Message ID and year must be numbers."); return
    except Exception: await event.respond("Could not read that storage-channel message."); return
    if not message or not message.media: await event.respond("That channel message does not contain media."); return
    data = {"title": title, "title_normalized": normalize_title(title), "year": year, "language": language, "quality": quality, "channel_id": STORAGE_CHANNEL_ID, "message_id": message_id, "filename": message.file.name if message.file else "Unknown", "created_at": datetime.now(timezone.utc), "enabled": True}
    await movies.update_one({"channel_id": STORAGE_CHANNEL_ID, "message_id": message_id}, {"$set": data}, upsert=True)
    await event.respond(f"✅ Added **{title} ({year})** — {language} — {quality}")

@client.on(events.NewMessage)
async def movie_search(event):
    if event.raw_text.startswith("/") or not (event.is_private or event.is_group): return
    query = clean_text(event.raw_text)
    if len(query) < 2: return
    results = await search_movies(query)
    if not results: await event.respond("😕 No matching movie found. Try another title."); return
    count = len(results)
    await event.respond(f"🎬 **{count} movie{'s' if count != 1 else ''} found**\n\nSelect your movie:", buttons=result_buttons(results))

@client.on(events.CallbackQuery(pattern=rb"^movie:([0-9a-f]{24})$"))
async def choose_movie(event):
    try: doc = await movies.find_one({"_id": ObjectId(event.data.decode().split(":", 1)[1]), "enabled": True})
    except Exception: doc = None
    if not doc: await event.answer("Movie is no longer available.", alert=True); return
    group_docs = await group_files(doc)
    if not group_docs: await event.answer("Movie files are no longer available.", alert=True); return
    languages = sorted({d.get("language", "Unknown") for d in group_docs}, key=str.lower)
    buttons = [Button.inline(lang[:50], data=f"lang:{doc['_id']}:{i}") for i, lang in enumerate(languages)]
    title = display_title(doc.get("title") or doc.get("filename") or "Movie"); year = doc.get("year") or "Year unknown"
    await event.edit(f"🎬 **{title} ({year})**\n📁 **{len(group_docs)} files found**\n\n🌐 **Select language:**", buttons=[buttons[i:i+2] for i in range(0, len(buttons), 2)]); await event.answer()

@client.on(events.CallbackQuery(pattern=rb"^lang:([0-9a-f]{24}):(\d+)$"))
async def choose_language(event):
    parts = event.data.decode().split(":"); movie_id, lang_index = parts[1], int(parts[2])
    try: base = await movies.find_one({"_id": ObjectId(movie_id), "enabled": True})
    except Exception: base = None
    if not base: await event.answer("Movie is no longer available.", alert=True); return
    group_docs = await group_files(base); languages = sorted({d.get("language", "Unknown") for d in group_docs}, key=str.lower)
    if lang_index >= len(languages): await event.answer("Language option expired. Search again.", alert=True); return
    language = languages[lang_index]
    qualities = sorted({d.get("quality", "Unknown") for d in group_docs if d.get("language", "Unknown") == language}, key=str.lower)
    buttons = [Button.inline(q[:50], data=f"quality:{movie_id}:{lang_index}:{i}") for i, q in enumerate(qualities)]
    title = display_title(base.get("title") or base.get("filename") or "Movie"); year = base.get("year") or "Year unknown"
    await event.edit(f"🎬 **{title} ({year})**\n📁 **{len(group_docs)} files found**\n🌐 Language: **{language}**\n\n📺 **Select quality:**", buttons=[buttons[i:i+2] for i in range(0, len(buttons), 2)]); await event.answer()

@client.on(events.CallbackQuery(pattern=rb"^quality:([0-9a-f]{24}):(\d+):(\d+)$"))
async def choose_quality(event):
    parts = event.data.decode().split(":"); movie_id, lang_index, quality_index = parts[1], int(parts[2]), int(parts[3])
    try: base = await movies.find_one({"_id": ObjectId(movie_id), "enabled": True})
    except Exception: base = None
    if not base: await event.answer("Movie is no longer available.", alert=True); return
    group_docs = await group_files(base); languages = sorted({d.get("language", "Unknown") for d in group_docs}, key=str.lower)
    if lang_index >= len(languages): await event.answer("Language option expired. Search again.", alert=True); return
    language = languages[lang_index]
    qualities = sorted({d.get("quality", "Unknown") for d in group_docs if d.get("language", "Unknown") == language}, key=str.lower)
    if quality_index >= len(qualities): await event.answer("Quality option expired. Search again.", alert=True); return
    quality = qualities[quality_index]
    matching = [d for d in group_docs if d.get("language", "Unknown") == language and d.get("quality", "Unknown") == quality]
    if not matching: await event.answer("That version is no longer available.", alert=True); return
    doc = matching[0]
    await event.edit("📤 **Preparing your file...** Please wait.")
    try:
        await client.forward_messages(event.sender_id, doc["message_id"], from_peer=STORAGE_CHANNEL_ID, drop_author=True)
        title = display_title(doc.get("title") or doc.get("filename") or "Movie"); year = doc.get("year") or "Year unknown"
        await event.edit(f"✅ **{title} ({year})**\n🌐 {language}  •  📺 {quality}\n\nFile sent above.")
    except (MessageIdInvalidError, ValueError):
        log.exception("Could not send stored media"); await event.edit("⚠️ I could not send that file. The stored channel message may be unavailable.")
    except Exception:
        log.exception("Unexpected send error"); await event.edit("⚠️ Something went wrong while sending the file. Please try again later.")
    await event.answer()

async def ensure_indexes():
    await movies.create_index([("title_normalized", 1), ("year", -1)])
    await movies.create_index([("channel_id", 1), ("message_id", 1)], unique=True)
    await movies.create_index([("enabled", 1), ("title_normalized", 1)])

async def main():
    Thread(target=run_health_server, daemon=True).start(); log.info("Health server started on port %s", os.getenv("PORT", "8000"))
    while True:
        try:
            await client.start(bot_token=BOT_TOKEN); break
        except FloodWaitError as exc:
            log.warning("Telegram authorization is rate-limited; waiting %s seconds before retrying.", exc.seconds); await asyncio.sleep(exc.seconds + 5)
    me = await client.get_me(); log.info("Bot started as @%s", me.username); await ensure_indexes(); log.info("Automatic channel indexing is enabled. Waiting for new storage-channel posts..."); await client.run_until_disconnected()

if __name__ == "__main__": asyncio.run(main())
import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import Button, TelegramClient, events
from telethon.errors import MessageIdInvalidError

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
SEARCH_LIMIT = 40
LANGUAGES = ("Tamil", "Telugu", "Malayalam", "Kannada", "Hindi", "English", "Bengali", "Marathi")
import_lock = asyncio.Lock()

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", value)
    return clean_text(value)

def parse_metadata(text: str):
    text = clean_text(text)
    year_match = re.search(r"(?:19|20)\d{2}", text)
    year = int(year_match.group()) if year_match else None
    languages = [x for x in LANGUAGES if re.search(rf"\b{re.escape(x)}\b", text, re.I)]
    quality_match = re.search(r"\b(2160p|4K|1080p|720p|480p|360p)\b", text, re.I)
    quality = quality_match.group(1) if quality_match else None
    if not quality:
        if re.search(r"\bweb[- ]?dl\b|\bwebdl\b", text, re.I): quality = "WEB-DL"
        elif re.search(r"\bhdrip\b", text, re.I): quality = "HDRip"
        elif re.search(r"\bbluray\b|\bbrrip\b", text, re.I): quality = "BluRay"
    title = re.sub(r"[._-]+", " ", text)
    title = re.sub(r"\b(2160p|4K|1080p|720p|480p|360p)\b", " ", title, flags=re.I)
    if year: title = re.sub(rf"\b{year}\b", " ", title)
    for lang in LANGUAGES: title = re.sub(rf"\b{re.escape(lang)}\b", " ", title, flags=re.I)
    return clean_text(title) or text, year, languages, quality

def metadata_from_message(message):
    caption = message.raw_text or ""
    filename = message.file.name if message.file else None
    source = filename or caption or "Unknown"
    title, year, languages, quality = parse_metadata(source)
    overrides = {"year": r"(?:year)\s*[:=-]\s*((?:19|20)\d{2})", "language": r"(?:language|lang)\s*[:=-]\s*([^\n|]+)", "quality": r"(?:quality)\s*[:=-]\s*([^\n|]+)"}
    for key, pattern in overrides.items():
        match = re.search(pattern, caption, re.I)
        if match:
            value = clean_text(match.group(1))
            if key == "year": year = int(value)
            elif key == "language":
                detected = [x for x in LANGUAGES if re.search(rf"\b{re.escape(x)}\b", value, re.I)]
                languages = list(dict.fromkeys(languages + detected)) or [value]
            else: quality = value
    return {"title": title, "title_normalized": normalize_title(title), "year": year, "languages": languages or ["Unknown"], "language": languages[0] if languages else "Unknown", "quality": quality or "Unknown", "channel_id": STORAGE_CHANNEL_ID, "message_id": message.id, "filename": filename or "Unknown", "created_at": datetime.now(timezone.utc), "enabled": True}

async def index_message(message):
    if not message.media or not message.file: return False
    data = metadata_from_message(message)
    await movies.update_one({"channel_id": STORAGE_CHANNEL_ID, "message_id": message.id}, {"$set": data}, upsert=True)
    log.info("Indexed message %s: %s | %s | %s | %s", message.id, data["title"], data["year"], ",".join(data["languages"]), data["quality"])
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

async def search_movie_groups(query: str):
    normalized = normalize_title(query)
    if not normalized: return []
    pattern = ".*" + ".*".join(re.escape(word) for word in normalized.split()) + ".*"
    pipeline = [{"$match": {"enabled": True, "title_normalized": {"$regex": pattern}}}, {"$group": {"_id": {"title_normalized": "$title_normalized", "year": "$year"}, "title": {"$first": "$title"}, "year": {"$first": "$year"}, "file_count": {"$sum": 1}, "representative_id": {"$first": "$_id"}}}, {"$sort": {"year": -1, "title": 1}}, {"$limit": SEARCH_LIMIT}]
    return await movies.aggregate(pipeline).to_list(length=SEARCH_LIMIT)

async def get_movie_group(movie_id: ObjectId):
    return await movies.find_one({"_id": movie_id, "enabled": True}, {"title": 1, "title_normalized": 1, "year": 1})

async def get_group_languages(base):
    docs = await movies.find({"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year")}, {"languages": 1, "language": 1}).to_list(length=SEARCH_LIMIT)
    found = set()
    for d in docs:
        vals = d.get("languages") or ([d.get("language")] if d.get("language") else [])
        if isinstance(vals, str): vals = [vals]
        found.update(clean_text(v) for v in vals if v and clean_text(v).lower() != "unknown")
    return sorted(found, key=str.lower)

async def get_group_file_count(base):
    return await movies.count_documents({"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year")})

async def get_language_docs(base, language):
    return await movies.find({"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year"), "$or": [{"languages": language}, {"language": language}]}).to_list(length=SEARCH_LIMIT)

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    await event.respond("🎬 **Movie Magic Club**\n\nSend a movie name here or in the movie group.\n\nI will show the matching movie/year first, then language → quality → file.")

@client.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel(event): await event.respond("❌ Search cancelled. Send a movie name to search again.")

@client.on(events.NewMessage)
async def movie_search(event):
    if event.raw_text.startswith("/") or not (event.is_private or event.is_group): return
    query = clean_text(event.raw_text)
    if len(query) < 2: return
    groups = await search_movie_groups(query)
    if not groups: await event.respond("😕 No matching movie found. Try another title."); return
    buttons = [[Button.inline(f"{clean_text(g.get('title') or 'Unknown title')} ({g.get('year') or 'Year unknown'}) • {g.get('file_count', 0)} files"[:64], data=f"movie:{g['representative_id']}")] for g in groups]
    await event.respond(f"🎬 **{len(groups)} movie{'s' if len(groups) != 1 else ''} found**\n\nSelect your movie:", buttons=buttons)

@client.on(events.CallbackQuery(pattern=rb"^movie:([0-9a-f]{24})$"))
async def choose_movie(event):
    try: movie_id = ObjectId(event.pattern_match.group(1).decode())
    except Exception: await event.answer("Invalid movie selection.", alert=True); return
    base = await get_movie_group(movie_id)
    if not base: await event.answer("Movie is no longer available.", alert=True); return
    languages = await get_group_languages(base)
    if not languages: await event.answer("No languages are available for this movie.", alert=True); return
    file_count = await get_group_file_count(base)
    buttons = [Button.inline(lang[:50], data=f"lang:{movie_id}:{i}") for i, lang in enumerate(languages)]
    year = base.get("year") or "Year unknown"
    await event.edit(f"🎬 **{base['title']} ({year})**\n📁 **{file_count} files found**\n\n🌐 **Select language:**", buttons=[buttons[i:i+2] for i in range(0,len(buttons),2)])
    await event.answer()

@client.on(events.CallbackQuery(pattern=rb"^lang:([0-9a-f]{24}):(\d+)$"))
async def choose_language(event):
    try: movie_id = ObjectId(event.pattern_match.group(1).decode()); lang_index = int(event.pattern_match.group(2).decode())
    except Exception: await event.answer("Invalid language selection.", alert=True); return
    base = await get_movie_group(movie_id)
    if not base: await event.answer("Movie is no longer available.", alert=True); return
    languages = await get_group_languages(base)
    if lang_index >= len(languages): await event.answer("Language option expired. Search again.", alert=True); return
    language = languages[lang_index]
    docs = await get_language_docs(base, language)
    qualities = sorted({clean_text(d.get("quality") or "Unknown") for d in docs}, key=str.lower)
    if not qualities: await event.answer("No quality is available for this language.", alert=True); return
    buttons = [Button.inline(q[:50], data=f"quality:{movie_id}:{lang_index}:{i}") for i,q in enumerate(qualities)]
    await event.edit(f"🎬 **{base['title']} ({base.get('year') or 'Year unknown'})**\n🌐 Language: **{language}**\n\n📺 **Select quality:**", buttons=[buttons[i:i+2] for i in range(0,len(buttons),2)])
    await event.answer()

@client.on(events.CallbackQuery(pattern=rb"^quality:([0-9a-f]{24}):(\d+):(\d+)$"))
async def choose_quality(event):
    try: movie_id = ObjectId(event.pattern_match.group(1).decode()); lang_index = int(event.pattern_match.group(2).decode()); quality_index = int(event.pattern_match.group(3).decode())
    except Exception: await event.answer("Invalid quality selection.", alert=True); return
    base = await get_movie_group(movie_id)
    if not base: await event.answer("Movie is no longer available.", alert=True); return
    languages = await get_group_languages(base)
    if lang_index >= len(languages): await event.answer("Language option expired. Search again.", alert=True); return
    language = languages[lang_index]
    docs = await get_language_docs(base, language)
    qualities = sorted({clean_text(d.get("quality") or "Unknown") for d in docs}, key=str.lower)
    if quality_index >= len(qualities): await event.answer("Quality option expired. Search again.", alert=True); return
    quality = qualities[quality_index]
    doc = await movies.find_one({"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year"), "$or": [{"languages": language}, {"language": language}], "quality": quality})
    if not doc: await event.answer("That version is no longer available.", alert=True); return
    await event.edit("📤 **Preparing your file...**\nPlease wait.")
    try:
        await client.forward_messages(event.sender_id, doc["message_id"], from_peer=STORAGE_CHANNEL_ID, drop_author=True)
        await event.edit(f"✅ **{base['title']} ({base.get('year') or 'Year unknown'})**\n🌐 {language} • 📺 {quality}\n\n📁 File sent above.")
    except (MessageIdInvalidError, ValueError):
        log.exception("Could not send stored media"); await event.edit("⚠️ I could not send that file. The stored channel message may be unavailable.")
    except Exception:
        log.exception("Unexpected send error"); await event.edit("⚠️ Something went wrong while sending the file. Please try again later.")
    await event.answer()

async def ensure_indexes():
    await movies.create_index([("title_normalized", 1), ("year", -1)])
    await movies.create_index([("channel_id", 1), ("message_id", 1)], unique=True)
    await movies.create_index([("enabled", 1), ("title_normalized", 1), ("year", -1)])

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info("Bot started as @%s", me.username)
    await ensure_indexes()
    log.info("Automatic channel indexing is enabled. Waiting for new storage-channel posts...")
    await client.run_until_disconnected()

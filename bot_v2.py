import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import Button, TelegramClient, events
from telethon.errors import MessageIdInvalidError

import verification

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
GROUP_DOC_LIMIT = 500  # docs read per movie/language when building buttons (must exceed max files per movie)
LANGUAGES = ("Tamil", "Telugu", "Malayalam", "Kannada", "Hindi", "English", "Bengali", "Marathi")
import_lock = asyncio.Lock()

JUNK_WORDS = {
    "video", "vid", "file", "document", "doc", "movie", "mp4", "mkv", "avi", "mov",
    "media", "photo", "img", "image", "whatsapp", "telegram", "download", "screenshot",
    "rec", "recording", "clip", "new", "final", "copy", "sample", "unknown", "wa",
}
# Upload/camera artifact tokens: pure numbers ("2024", "0002") or letter-prefix+numbers ("WA0002").
JUNK_TOKEN_RE = re.compile(r"(?i)^(?:\d+|[a-z]{1,4}\d{3,}[a-z0-9]*)$")

def is_junk_title(title: str) -> bool:
    """True when a parsed title is clearly a camera/upload artifact, not a movie name."""
    if not title or title in ("Unknown", "Unknown title"):
        return True
    tokens = title.split()
    return all(t.lower() in JUNK_WORDS or JUNK_TOKEN_RE.match(t) for t in tokens)

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
    caption = clean_text(message.raw_text or "")
    filename = message.file.name if message.file else None
    source = filename or caption or "Unknown"
    title, year, languages, quality = parse_metadata(source)
    # ALWAYS mine the caption as well. Telegram filenames are often missing or
    # junk ("video_2024-01-15.mp4") while the caption carries the real release
    # name. Languages are UNIONED so multilingual releases ("[Malayalam +
    # Kannada]") keep every language no matter where it was written.
    if caption and caption != source:
        caption_title, caption_year, caption_languages, caption_quality = parse_metadata(caption)
        languages = list(dict.fromkeys(list(languages or []) + list(caption_languages or [])))
        if is_junk_title(title) and not is_junk_title(caption_title):
            # The filename was an upload artifact ("VID-20231101-WA0002.mp4"):
            # trust the caption wholesale, including its year and quality.
            title = caption_title
            year = caption_year or year
            quality = caption_quality or quality
        else:
            if not year:
                year = caption_year
            if not quality:
                quality = caption_quality
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
    return {"title": title, "title_normalized": normalize_title(title), "year": year, "languages": languages or ["Unknown"], "language": languages[0] if languages else "Unknown", "quality": quality or "Unknown", "channel_id": STORAGE_CHANNEL_ID, "message_id": message.id, "filename": filename or "Unknown", "caption": caption[:500] if caption else None, "created_at": datetime.now(timezone.utc), "enabled": True}

async def index_message(message):
    if not message.media or not message.file: return False
    data = metadata_from_message(message)
    created_at = data.pop("created_at")
    await movies.update_one({"channel_id": STORAGE_CHANNEL_ID, "message_id": message.id}, {"$set": data, "$setOnInsert": {"created_at": created_at}}, upsert=True)
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

# Words users type that are never part of the stored movie title — stripped from
# queries so "leo movie", "leo tamil", "leo 1080p" all find Leo.
SEARCH_NOISE_WORDS = {
    "movie", "movies", "film", "films", "full", "download", "watch", "online",
    "free", "link", "links", "file", "files", "print", "new", "latest", "hd",
    "dub", "dubbed", "dual", "audio", "part", "cd1", "cd2",
    "1080p", "720p", "480p", "360p", "2160p", "4k", "uhd", "fhd",
    "hdrip", "webdl", "web", "webrip", "bluray", "brrip", "dvdrip", "hdtv", "cam",
} | {lang.lower() for lang in LANGUAGES}

def parse_search_query(query: str):
    """Split a user query into (title_words, year_hint).

    "leo" -> (["leo"], None)
    "leo 2023" / "leo movie 2023" / "2023 leo tamil" -> (["leo"], 2023)
    "leo tamil" -> (["leo"], None)
    "2018" (a real movie title) -> (["2018"], None)  # lone number stays a title
    """
    normalized = normalize_title(query)
    words = normalized.split()
    if not words:
        return [], None
    year = None
    if len(words) > 1:
        for w in words:
            if re.fullmatch(r"(?:19|20)\d{2}", w):
                year = int(w)
                break
    rest = [w for w in words if str(year) != w] if year is not None else words
    title_words = [w for w in rest if w not in SEARCH_NOISE_WORDS]
    if not title_words:
        # query was only noise words (e.g. "tamil movie") — keep it literal
        title_words = rest or words
        if not rest:
            year = None
    return title_words, year

def movie_groups_pipeline(pattern: str, year=None):
    match = {"enabled": True, "title_normalized": {"$regex": pattern}}
    if year is not None:
        match["year"] = year
    return [
        {"$match": match},
        {"$group": {"_id": {"title_normalized": "$title_normalized", "year": "$year"}, "title": {"$first": "$title"}, "year": {"$first": "$year"}, "file_count": {"$sum": 1}, "representative_id": {"$first": "$_id"}}},
        {"$sort": {"year": -1, "title": 1}},
        {"$limit": SEARCH_LIMIT},
    ]

async def search_movie_groups(query: str):
    title_words, year = parse_search_query(query)
    if not title_words:
        return []
    pattern = ".*" + ".*".join(re.escape(word) for word in title_words) + ".*"
    if year is not None:
        groups = await movies.aggregate(movie_groups_pipeline(pattern, year)).to_list(length=SEARCH_LIMIT)
        if groups:
            return groups
        # no exact-year match — fall back to title-only so a wrong year hint
        # never hides the movie entirely
    return await movies.aggregate(movie_groups_pipeline(pattern, None)).to_list(length=SEARCH_LIMIT)

async def get_movie_group(movie_id: ObjectId):
    return await movies.find_one({"_id": movie_id, "enabled": True}, {"title": 1, "title_normalized": 1, "year": 1})

async def get_group_languages(base):
    docs = await movies.find({"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year")}, {"languages": 1, "language": 1}).to_list(length=GROUP_DOC_LIMIT)
    found = set()
    for d in docs:
        vals = d.get("languages") or ([d.get("language")] if d.get("language") else [])
        if isinstance(vals, str): vals = [vals]
        found.update(clean_text(v) for v in vals if v and clean_text(v).lower() != "unknown")
    return sorted(found, key=str.lower)

async def get_group_file_count(base):
    return await movies.count_documents({"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year")})

async def get_language_docs(base, language):
    return await movies.find({"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year"), "$or": [{"languages": language}, {"language": language}]}).to_list(length=GROUP_DOC_LIMIT)

@client.on(events.NewMessage(pattern=r"^/start(?:\s+(\S+))?$"))
async def start(event):
    payload = event.pattern_match.group(1)
    if payload:
        # deep link back from the verification success page (or any /start payload)
        await event.respond(await verification.status_text(sys.modules[__name__], event.sender_id))
        return
    await event.respond("🎬 **Movie Magic Club**\n\nSend a movie name here or in the movie group.\n\nI will show the matching movie/year first, then language → quality → file.")

@client.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel(event): await event.respond("❌ Search cancelled. Send a movie name to search again.")

async def send_chunked(event, header, lines):
    buffer = header
    for line in lines:
        if len(buffer) + len(line) + 1 > 3800:
            await event.respond(buffer)
            buffer = ""
        buffer += line + "\n"
    if buffer.strip():
        await event.respond(buffer)

@client.on(events.NewMessage(pattern=r"^/check(?:\s+(.+))?$"))
async def check_command(event):
    """Admin diagnostic: show the ACTUAL stored MongoDB records for a movie.

    This is the source-of-truth inspection for debugging metadata problems —
    no guessing about what the database contains.
    """
    if event.sender_id not in ADMIN_IDS:
        await event.respond("⛔ Admin-only command. Add your Telegram user ID to `ADMIN_IDS` on Koyeb, redeploy, and try again.")
        return
    query = clean_text(event.pattern_match.group(1) or "")
    if not query:
        await event.respond("Usage: `/check <movie name>` — shows the real stored records."); return
    normalized = normalize_title(query)
    pattern = ".*" + ".*".join(re.escape(w) for w in normalized.split()) + ".*"
    docs = await movies.find({"enabled": True, "title_normalized": {"$regex": pattern}}).limit(25).to_list(length=25)
    total = await movies.count_documents({"enabled": True, "title_normalized": {"$regex": pattern}})
    from repair import count_broken
    broken = await count_broken(sys.modules[__name__])
    if not docs:
        await event.respond(f"No records match `{query}`.")
        if broken:
            await event.respond(f"⚠️ There are **{broken}** broken records (Unknown language/title) that this search can never see.\nRun `/repair` to rebuild them from the storage channel.")
        return
    lines = []
    for d in docs:
        lines.append(
            f"• msg `{d.get('message_id')}` | **{d.get('title')}** ({d.get('year')})\n"
            f"  langs=`{d.get('languages')}` lang=`{d.get('language')}` quality=`{d.get('quality')}`\n"
            f"  file=`{str(d.get('filename'))[:70]}` caption_stored=`{'yes' if d.get('caption') else 'no'}`"
        )
    footer = f"\nℹ️ Broken records invisible to this search: **{broken}**" if broken else ""
    await send_chunked(event, f"🔍 **{total}** record(s) match `{query}` (showing up to 25):{footer}\n", lines)

@client.on(events.NewMessage(pattern=r"^/repair(?:\s+(all))?$"))
async def repair_command(event):
    """Admin: rebuild metadata from the actual storage-channel messages.

    Targeted mode (default) only rebuilds broken records (Unknown language/title).
    `/repair all` re-reads the whole channel. Nothing is deleted; records are
    updated in place, keyed by (channel_id, message_id) — no duplicates.
    """
    if event.sender_id not in ADMIN_IDS:
        await event.respond("⛔ Admin-only command. Add your Telegram user ID to `ADMIN_IDS` on Koyeb, redeploy, and try again.")
        return
    if import_lock.locked():
        await event.respond("⏳ Another import/repair is already running. Wait for it to finish."); return
    full = bool(event.pattern_match.group(1))
    mode = "FULL channel rescan" if full else "targeted repair of broken records"
    await event.respond(
        f"🛠 Starting **{mode}**...\n\n"
        "Metadata is rebuilt from the actual storage-channel messages (filename + caption).\n"
        "Nothing is deleted or re-imported from scratch — records are updated in place."
    )
    async def progress(processed, updated):
        try: await event.respond(f"🛠 Repair progress: processed **{processed}**, updated **{updated}** records.")
        except Exception: pass
    async with import_lock:
        try:
            from repair import repair_from_source
            stats = await repair_from_source(sys.modules[__name__], full=full, progress=progress)
            await event.respond(
                f"✅ **Repair complete.**\n\n"
                f"Mode: {stats['mode']}\nTargets: {stats['targets']}\n"
                f"Processed: **{stats['processed']}**\nUpdated: **{stats['updated']}**\n\n"
                "Now try the movie search again."
            )
        except Exception as exc:
            log.exception("Repair failed")
            await event.respond(f"❌ Repair failed: `{type(exc).__name__}: {exc}`")

@client.on(events.NewMessage(pattern=r"^/stats$"))
async def stats_command(event):
    if event.sender_id not in ADMIN_IDS:
        await event.respond("⛔ Admin-only command. Add your Telegram user ID to `ADMIN_IDS` on Koyeb, redeploy, and try again.")
        return
    total = await movies.count_documents({"enabled": True})
    groups = await movies.aggregate([{"$match": {"enabled": True}}, {"$group": {"_id": {"t": "$title_normalized", "y": "$year"}}}]).to_list(length=500000)
    language_counts = {}
    quality_counts = {}
    async for d in movies.find({"enabled": True}, {"languages": 1, "language": 1, "quality": 1}):
        langs = d.get("languages") or ([d.get("language")] if d.get("language") else [])
        if isinstance(langs, str): langs = [langs]
        for lang in langs:
            if lang: language_counts[lang] = language_counts.get(lang, 0) + 1
        quality = d.get("quality") or "Unknown"
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
    top_languages = sorted(language_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    top_qualities = sorted(quality_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    from repair import count_broken
    broken = await count_broken(sys.modules[__name__])
    lines = [
        "📊 **Bot statistics**\n",
        f"📁 Indexed files (enabled): **{total}**",
        f"🎬 Unique movies (title+year): **{len(groups)}**",
        f"⚠️ Broken records (Unknown language/title): **{broken}**",
        "\n🌐 Languages:",
        *[f"  • {k}: {v}" for k, v in top_languages],
        "\n📺 Qualities:",
        *[f"  • {k}: {v}" for k, v in top_qualities],
    ]
    await event.respond("\n".join(lines))

def _admin_only(event):
    return event.sender_id in ADMIN_IDS

async def _reject_non_admin(event):
    await event.respond("⛔ Admin-only command. Add your Telegram user ID to `ADMIN_IDS` on Koyeb, redeploy, and try again.")

@client.on(events.NewMessage(pattern=r"^/verifyon$"))
async def verify_on(event):
    if not _admin_only(event): await _reject_non_admin(event); return
    await verification.set_setting(sys.modules[__name__], "enabled", True)
    await event.respond("✅ Shortlink verification is now **ON**.")

@client.on(events.NewMessage(pattern=r"^/verifyoff$"))
async def verify_off(event):
    if not _admin_only(event): await _reject_non_admin(event); return
    await verification.set_setting(sys.modules[__name__], "enabled", False)
    await event.respond("⏸ Shortlink verification is now **OFF** — all users get unlimited files.")

@client.on(events.NewMessage(pattern=r"^/verifylimit\s+(\d+)$"))
async def verify_limit(event):
    if not _admin_only(event): await _reject_non_admin(event); return
    limit = int(event.pattern_match.group(1))
    await verification.set_setting(sys.modules[__name__], "free_limit", limit)
    await event.respond(f"✅ Free daily limit set to **{limit}** files per user.")

@client.on(events.NewMessage(pattern=r"^/verifyhours\s+(\d+)$"))
async def verify_hours(event):
    if not _admin_only(event): await _reject_non_admin(event); return
    hours = int(event.pattern_match.group(1))
    await verification.set_setting(sys.modules[__name__], "valid_hours", hours)
    await event.respond(f"✅ Verification validity set to **{hours}** hours of unlimited access.")

@client.on(events.NewMessage(pattern=r"^/verifystatus$"))
async def verify_status(event):
    if not _admin_only(event): await _reject_non_admin(event); return
    bot = sys.modules[__name__]
    settings = await verification.get_settings(bot)
    now = verification.utcnow()
    total_users = await db.verifications.count_documents({})
    verified_now = await db.verifications.count_documents({"verified_until": {"$gt": now}})
    base_url = verification.public_base_url() or "(not set — gate will fail open!)"
    shortlink = "configured" if settings["shortlink_api"] and settings["shortlink_url"] else "NOT configured (raw links will be used)"
    state = await verification.get_state(bot, event.sender_id)
    await event.respond(
        "🔐 **Verification settings**\n\n"
        f"Enabled: **{'ON' if settings['enabled'] else 'OFF'}**\n"
        f"Free daily limit: **{settings['free_limit']}** files\n"
        f"Verified window: **{settings['valid_hours']}** hours\n"
        f"Shortlink API: **{shortlink}**\n"
        f"Public base URL: `{base_url}`\n\n"
        f"👥 Users tracked: **{total_users}**\n"
        f"✅ Currently verified: **{verified_now}**\n"
        f"🙋 Your state: free_used=**{state.get('free_used', 0)}**, verified_until=`{state.get('verified_until') or '—'}`\n\n"
        "⚠️ You are in ADMIN_IDS — **the gate never applies to you.** "
        "Test with a normal account, or use /verifytest to simulate a normal user with your state.\n\n"
        "Commands: /verifyon /verifyoff /verifylimit N /verifyhours N /verifystatus /verifytest /verifyreset"
    )

@client.on(events.NewMessage(pattern=r"^/verifytest$"))
async def verify_test(event):
    """Admin: simulate the gate as a normal user with your own current state."""
    if not _admin_only(event): await _reject_non_admin(event); return
    bot = sys.modules[__name__]
    settings = await verification.get_settings(bot)
    state = await verification.get_state(bot, event.sender_id)
    blocked = verification.is_blocked(settings, state)
    used = int(state.get("free_used", 0))
    vu = state.get("verified_until")
    lines = [
        "🧪 **Verification self-test** — what a NORMAL user would get with your current state:\n",
        f"• Verification: **{'ON' if settings['enabled'] else 'OFF'}**",
        f"• Free limit: **{settings['free_limit']}** files/day",
        f"• Your free_used today: **{used}**",
        f"• Your verified_until: `{vu or 'none'}`",
        f"\n→ A normal user would be: **{'🚫 BLOCKED — gate shown' if blocked else '✅ ALLOWED — file sent'}**",
    ]
    if blocked:
        link = await verification.build_verification_link(bot, event.sender_id, settings)
        if link:
            lines.append("\nGate preview (real link):")
            await event.respond("\n".join(lines), buttons=[[Button.url("✅ Verify & Get Unlimited", link)]])
            return
        lines.append("\n⚠️ Could not build a verification link — BASE_URL/KOYEB_PUBLIC_DOMAIN missing!")
    await event.respond("\n".join(lines))

@client.on(events.NewMessage(pattern=r"^/verifyreset(?:\s+(\d+))?$"))
async def verify_reset(event):
    """Admin: reset a user's verification state (default: yourself)."""
    if not _admin_only(event): await _reject_non_admin(event); return
    target = int(event.pattern_match.group(1)) if event.pattern_match.group(1) else event.sender_id
    await db.verifications.update_one(
        {"_id": target},
        {"$set": {"free_used": 0, "verified_until": None, "updated_at": verification.utcnow()}},
        upsert=True,
    )
    await event.respond(f"♻️ Verification state reset for user `{target}` (0 files used, not verified).")

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
    rows = [buttons[i:i+2] for i in range(0,len(buttons),2)]
    rows.append([Button.inline("« Back to languages", data=f"movie:{movie_id}")])
    await event.edit(f"🎬 **{base['title']} ({base.get('year') or 'Year unknown'})**\n🌐 Language: **{language}**\n\n📺 **Select quality:**", buttons=rows)
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
    file_filter = {"enabled": True, "title_normalized": base["title_normalized"], "year": base.get("year"), "$or": [{"languages": language}, {"language": language}], "quality": quality}
    docs = await movies.find(file_filter).sort("message_id", 1).limit(10).to_list(length=10)
    total_matching = await movies.count_documents(file_filter)
    if not docs: await event.answer("That version is no longer available.", alert=True); return

    # Shortlink verification gate: FREE_LIMIT free deliveries, then verify for unlimited.
    if event.sender_id not in ADMIN_IDS:
        allowed, gate = await verification.check_access(sys.modules[__name__], event.sender_id)
        if not allowed:
            buttons = [
                [Button.url("✅ Verify & Get Unlimited", gate["link"])],
                [Button.inline("« Back to languages", data=f"movie:{movie_id}")],
            ]
            await event.edit(
                "🚫 **Today's free limit is over!**\n\n"
                f"You have already used your **{gate['free_limit']} free files** for today.\n\n"
                f"✅ Complete one quick verification and get **UNLIMITED movies for {gate['valid_hours']} hours**:\n"
                "👉 Tap **Verify & Get Unlimited** below, finish the short link, then come back and tap your quality again.\n\n"
                "⏳ Or just wait — your free limit resets automatically.",
                buttons=buttons,
            )
            await event.answer()
            return

    await event.edit(f"📤 **Preparing your file{'s' if len(docs) != 1 else ''}...**\nPlease wait.")
    sent = 0
    for doc in docs:
        try:
            await client.forward_messages(event.sender_id, doc["message_id"], from_peer=STORAGE_CHANNEL_ID, drop_author=True)
            sent += 1
        except (MessageIdInvalidError, ValueError):
            log.warning("Stored channel message %s is unavailable", doc.get("message_id"))
        except Exception:
            log.exception("Unexpected send error for message %s", doc.get("message_id"))
    if sent and event.sender_id not in ADMIN_IDS:
        await verification.record_delivery(sys.modules[__name__], event.sender_id, sent)
    year = base.get("year") or "Year unknown"
    nav = [[Button.inline("« Qualities", data=f"lang:{movie_id}:{lang_index}"), Button.inline("« Languages", data=f"movie:{movie_id}")]]
    if sent:
        extra = f"\n(Showing first {len(docs)} of {total_matching} matching files.)" if total_matching > len(docs) else ""
        await event.edit(f"✅ **{base['title']} ({year})**\n🌐 {language} • 📺 {quality}\n\n📁 Sent **{sent}** file{'s' if sent != 1 else ''} above.{extra}", buttons=nav)
    else:
        await event.edit("⚠️ I could not send those files. The stored channel messages may be unavailable.", buttons=nav)
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

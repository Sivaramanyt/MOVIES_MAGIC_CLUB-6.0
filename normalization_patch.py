import re
from datetime import datetime, timezone

NOISE_WORDS = (
    "www", "tamilmv", "tamilblasters", "1tamilblasters", "tamilrockers", "isaimini",
    "hdt", "nf", "webdl", "web", "dl", "webrip", "bluray", "brrip", "hdrip",
    "dvdrip", "dvdscr", "hdcam", "cam", "proper", "hq", "true", "untouched",
    "remux", "x264", "x265", "hevc", "avc", "aac", "dd", "dd5", "ddp", "ddp5",
    "atmos", "mkv", "mp4", "avi", "mov", "dual", "audio", "multi", "sample",
    "repack", "rip", "subs", "subtitle", "re", "original", "unrated", "extended",
    "directors", "cut", "version", "movie",
)
LANGUAGES = ("Tamil", "Telugu", "Malayalam", "Kannada", "Hindi", "English", "Bengali", "Marathi")


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", value)
    return clean_text(value)


def canonical_movie_title(value):
    text = clean_text(value).lower()
    text = re.sub(r"@[A-Za-z0-9_]+", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:tamilmv|tamilblasters|1tamilblasters|tamilrockers|isaimini)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b(?:2160p|1080p|720p|480p|360p|4k|uhd|fhd|hd)\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:gb|gib|mb|mib)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:" + "|".join(re.escape(x) for x in NOISE_WORDS) + r")\b", " ", text, flags=re.I)
    text = re.sub(r"\bx\d+\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"[._+\-]+", " ", text)
    text = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", text)
    return clean_text(text)


def parse_metadata(text):
    text = clean_text(text)
    year_match = re.search(r"(?:19|20)\d{2}", text)
    year = int(year_match.group()) if year_match else None
    language = next((x for x in LANGUAGES if re.search(rf"\b{re.escape(x)}\b", text, re.I)), None)
    quality_match = re.search(r"\b(2160p|4K|1080p|720p|480p|360p)\b", text, re.I)
    quality = quality_match.group(1) if quality_match else None
    title = canonical_movie_title(text)
    return title or clean_text(text), year, language, quality


def install(bot):
    bot.parse_metadata = parse_metadata


async def migrate_existing_movies(bot):
    # v3 deliberately reruns normalization even if v1/v2 markers exist.
    marker = bot.db.movie_magic_meta
    marker_id = "title_normalization_v3"
    if await marker.find_one({"_id": marker_id}):
        return 0

    updated = 0
    scanned = 0
    cursor = bot.movies.find({"enabled": True}, {"_id": 1, "title": 1, "filename": 1, "year": 1})
    async for doc in cursor:
        scanned += 1
        source = doc.get("filename") or doc.get("title") or ""
        title = canonical_movie_title(source) or canonical_movie_title(doc.get("title") or "")
        if not title:
            continue
        normalized = normalize_title(title)
        if doc.get("title") != title or doc.get("title_normalized") != normalized:
            await bot.movies.update_one(
                {"_id": doc["_id"]},
                {"$set": {"title": title, "title_normalized": normalized, "title_normalized_at": datetime.now(timezone.utc)}},
            )
            updated += 1

    await marker.update_one(
        {"_id": marker_id},
        {"$set": {"completed_at": datetime.now(timezone.utc), "scanned": scanned, "updated": updated}},
        upsert=True,
    )
    return updated

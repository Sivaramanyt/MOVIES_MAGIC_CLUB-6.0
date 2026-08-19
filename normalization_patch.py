import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pymongo import UpdateOne

NOISE_WORDS = (
    "www", "tamilmv", "tamilblasters", "1tamilblasters", "tamilrockers", "isaimini",
    "hdt", "nf", "webdl", "web", "dl", "webrip", "bluray", "brrip", "hdrip",
    "dvdrip", "dvdscr", "hdcam", "cam", "proper", "hq", "true", "untouched", "untouche",
    "remux", "x264", "x265", "hevc", "avc", "aac", "dd", "dd5", "ddp", "ddp5",
    "atmos", "mkv", "mp4", "avi", "mov", "dual", "audio", "multi", "sample",
    "repack", "rip", "subs", "subtitle", "re", "original", "unrated", "extended",
    "directors", "cut", "version", "movie", "release", "team", "esub", "m", "192kbps",
    "128kbps", "256kbps", "320kbps", "kbps",
)
LANGUAGES = ("Tamil", "Telugu", "Malayalam", "Kannada", "Hindi", "English", "Bengali", "Marathi")


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", value)
    return clean_text(value)


def detect_languages(text):
    text = clean_text(text)
    return [lang for lang in LANGUAGES if re.search(rf"\b{re.escape(lang)}\b", text, re.I)]


def canonical_movie_title(value):
    text = clean_text(value).lower()
    text = re.sub(r"@[A-Za-z0-9_]+", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\((?:19|20)?\d{0,4}\)", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b(?:2160p|1080p|720p|480p|360p|4k|uhd|fhd|hd)\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:gb|gib|mb|mib)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:" + "|".join(re.escape(x) for x in LANGUAGES) + r")\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:" + "|".join(re.escape(x) for x in NOISE_WORDS) + r")\b", " ", text, flags=re.I)
    text = re.sub(r"\bx\d+\b", " ", text, flags=re.I)
    text = re.sub(r"\bx\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"[._+\-]+", " ", text)
    text = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", text)
    return clean_text(text)


def display_title(value):
    title = canonical_movie_title(value)
    if not title:
        return "Unknown title"
    return " ".join(word[:1].upper() + word[1:] for word in title.split())


def parse_metadata(text):
    text = clean_text(text)
    year_match = re.search(r"(?:19|20)\d{2}", text)
    year = int(year_match.group()) if year_match else None
    languages = detect_languages(text)
    quality_match = re.search(r"\b(2160p|4K|1080p|720p|480p|360p)\b", text, re.I)
    if quality_match:
        quality = quality_match.group(1)
    elif re.search(r"\bweb[- ]?dl\b|\bwebdl\b", text, re.I):
        quality = "WEB-DL"
    elif re.search(r"\bhdrip\b", text, re.I):
        quality = "HDRip"
    elif re.search(r"\bbluray\b|\bbrrip\b", text, re.I):
        quality = "BluRay"
    else:
        quality = None
    title = canonical_movie_title(text)
    return display_title(title), year, languages, quality


def install(bot):
    bot.parse_metadata = parse_metadata
    bot.normalize_title = normalize_title
    bot.detect_languages = detect_languages


async def migrate_existing_movies(bot):
    """Rebuild titles/languages for existing records using batched MongoDB writes."""
    marker = bot.db.movie_magic_meta
    marker_id = "title_normalization_v6_multilanguage_variants"
    if await marker.find_one({"_id": marker_id}):
        bot.log.info("Movie title normalization v6 was already completed; skipping.")
        return 0

    bot.log.info("Loading existing movie records for v6 normalization...")
    docs = []
    cursor = bot.movies.find(
        {"enabled": True},
        {"_id": 1, "title": 1, "filename": 1, "year": 1, "language": 1, "languages": 1, "quality": 1},
    )
    async for doc in cursor:
        docs.append(doc)

    total = len(docs)
    bot.log.info("v6 normalization found %s existing records.", total)

    known_years = defaultdict(Counter)
    prepared = []
    for doc in docs:
        source = doc.get("filename") or doc.get("title") or ""
        parsed_title, parsed_year, parsed_languages, parsed_quality = parse_metadata(source)
        if parsed_title == "Unknown title":
            parsed_title = display_title(doc.get("title") or source)
        year = doc.get("year") or parsed_year
        languages = parsed_languages
        if not languages:
            old_languages = doc.get("languages") or []
            if isinstance(old_languages, str):
                old_languages = [old_languages]
            languages = [x for x in old_languages if x] or ([doc.get("language")] if doc.get("language") else [])
        languages = list(dict.fromkeys(languages)) or ["Unknown"]
        if year:
            known_years[normalize_title(parsed_title)][int(year)] += 1
        prepared.append((doc, parsed_title, year, languages, parsed_quality))

    updated = 0
    now = datetime.now(timezone.utc)
    batch = []
    batch_size = 250

    async def flush_batch():
        nonlocal updated, batch
        if not batch:
            return
        result = await bot.movies.bulk_write(batch, ordered=False)
        updated += result.modified_count
        batch = []
        bot.log.info("v6 normalization progress: %s/%s records processed; %s updated.", processed, total, updated)

    processed = 0
    for doc, title, year, languages, parsed_quality in prepared:
        key = normalize_title(title)
        if not year and key in known_years and len(known_years[key]) == 1:
            year = next(iter(known_years[key]))

        update = {
            "title": title,
            "title_normalized": key,
            "languages": languages,
            "language": languages[0],
            "title_normalized_at": now,
        }
        if year and doc.get("year") != year:
            update["year"] = int(year)
        if (not doc.get("quality") or doc.get("quality") == "Unknown") and parsed_quality:
            update["quality"] = parsed_quality

        if any(doc.get(k) != v for k, v in update.items() if k != "title_normalized_at"):
            batch.append(UpdateOne({"_id": doc["_id"]}, {"$set": update}))
        processed += 1
        if len(batch) >= batch_size:
            await flush_batch()

    await flush_batch()

    await marker.update_one(
        {"_id": marker_id},
        {"$set": {"completed_at": now, "scanned": total, "updated": updated}},
        upsert=True,
    )
    bot.log.info("Movie title normalization complete; scanned %s, updated %s existing records.", total, updated)
    return updated

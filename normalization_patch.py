import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pymongo import UpdateOne

NOISE_WORDS = (
    "www", "tamilmv", "tamilblasters", "1tamilblasters", "tamilrockers", "isaimini",
    "hdt", "hdtv", "nf", "webdl", "web", "dl", "webrip", "bluray", "brrip", "hdrip",
    "dvdrip", "dvdscr", "predvd", "hdcam", "cam", "camrip", "proper", "hq", "true",
    "untouched", "untouche", "remux", "x264", "x265", "hevc", "avc", "aac", "aac2",
    "ac3", "eac3", "dts", "ma", "hdma", "truehd", "lpcm", "pcm", "dd", "dd5", "ddp",
    "ddp5", "atmos", "mkv", "mp4", "avi", "mov", "dual", "audio", "multi", "sample",
    "repack", "rip", "subs", "msubs", "subtitle", "re", "original", "unrated", "extended",
    "directors", "cut", "version", "movie", "release", "team", "esub", "esubs", "full",
    "uncut", "m", "com", "net", "org", "192kbps", "128kbps", "256kbps", "320kbps", "kbps",
)
LANGUAGES = ("Tamil", "Telugu", "Malayalam", "Kannada", "Hindi", "English", "Bengali", "Marathi")

def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()

def space_separators(text):
    """Turn underscores/dots/hyphens/pluses into spaces FIRST.

    Regex \\b does not fire between underscores ("1080P_HD" has no word
    boundary around 1080P), so every \\b-based cleaner silently failed on
    underscore-separated release names. Normalizing separators first makes
    all downstream patterns work on every naming style.
    """
    return re.sub(r"[._+\-]+", " ", text or "")

def detect_languages(text):
    text = clean_text(text)
    found = []
    for lang in LANGUAGES:
        if re.search(rf"(?<![A-Za-z]){re.escape(lang)}(?![A-Za-z])", text, re.I):
            found.append(lang)
    return found

def detect_quality(text):
    """Resolution labels win (1080p/720p...); rip/source types are the fallback."""
    t = space_separators(clean_text(text)).lower()
    if re.search(r"\b(2160p|4k|uhd)\b", t):
        return "2160p"
    if re.search(r"\b(1080p|fhd|full hd)\b", t):
        return "1080p"
    if re.search(r"\b720p\b", t):
        return "720p"
    if re.search(r"\b(480p|sd)\b", t):
        return "480p"
    if re.search(r"\b360p\b", t):
        return "360p"
    if re.search(r"\bweb ?dl\b", t):
        return "WEB-DL"
    if re.search(r"\bhdrip\b", t):
        return "HDRip"
    if re.search(r"\bhdtv\b", t):
        return "HDTV"
    if re.search(r"\bwebrip\b", t):
        return "WEBRip"
    if re.search(r"\b(bluray|brrip|blu ray)\b", t):
        return "BluRay"
    if re.search(r"\b(dvdrip|dvdscr|predvd|dvd)\b", t):
        return "DVDRip"
    if re.search(r"\b(hdcam|camrip|cam)\b", t):
        return "CAM"
    if re.search(r"\bhd\b", t):
        return "720p"  # bare "HD" convention when nothing else is present
    return None

def normalize_title(value):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", value)
    return clean_text(value)

def canonical_movie_title(value):
    text = clean_text(value).lower()
    text = re.sub(r"@[A-Za-z0-9_]+", " ", text)  # mentions first, while still glued ("@Movies_Magic_club")
    text = space_separators(text)  # then split separators: makes \b patterns work on any naming style
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\((?:19|20)?\d{0,4}\)", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b(?:2160p|1080p|720p|480p|360p|4k|uhd|fhd|hd)\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:gb|gib|mb|mib)\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:kbps|mbps|bps)\b", " ", text, flags=re.I)  # audio bitrates (640kbps etc.)
    text = re.sub(r"\b(?:part|pt|cd|vol|disc|disk)\s*\d{1,3}\b", " ", text, flags=re.I)  # multi-part tags (Part001, CD2...)
    text = re.sub(r"\b(?:" + "|".join(re.escape(x) for x in LANGUAGES) + r")\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:" + "|".join(re.escape(x) for x in NOISE_WORDS) + r")\b", " ", text, flags=re.I)
    text = re.sub(r"\bx\d+\b", " ", text, flags=re.I)
    text = re.sub(r"\bx\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+\b", " ", text)
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
    quality = detect_quality(text)
    title = canonical_movie_title(text)
    return display_title(title), year, languages, quality

def install(bot):
    bot.parse_metadata = parse_metadata
    bot.normalize_title = normalize_title
    bot.detect_languages = detect_languages

async def migrate_existing_movies(bot):
    """Force a fresh language/title rebuild from every stored original filename."""
    marker = bot.db.movie_magic_meta
    marker_id = "title_normalization_v8_multilanguage_filename_detection"
    if await marker.find_one({"_id": marker_id}):
        bot.log.info("Movie title normalization v8 was already completed; skipping.")
        return 0

    bot.log.info("Loading existing movie records for v8 language normalization...")
    cursor = bot.movies.find(
        {"enabled": True},
        {"_id": 1, "title": 1, "filename": 1, "year": 1, "language": 1, "languages": 1, "quality": 1},
    )
    total = 0
    updated = 0
    batch = []
    batch_size = 250
    known_years = defaultdict(Counter)
    prepared = []

    async for doc in cursor:
        total += 1
        source = doc.get("filename") or doc.get("title") or ""
        parsed_title, parsed_year, parsed_languages, parsed_quality = parse_metadata(source)
        year = doc.get("year") or parsed_year
        if not parsed_languages:
            old = doc.get("languages") or []
            if isinstance(old, str):
                old = [old]
            parsed_languages = [x for x in old if x and str(x).lower() != "unknown"]
        parsed_languages = list(dict.fromkeys(parsed_languages)) or ["Unknown"]
        if year:
            known_years[normalize_title(parsed_title)][int(year)] += 1
        prepared.append((doc, parsed_title, year, parsed_languages, parsed_quality))

    bot.log.info("v8 normalization found %s existing records.", total)
    processed = 0
    now = datetime.now(timezone.utc)
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
            "language_normalized_at": now,
        }
        if year and doc.get("year") != year:
            update["year"] = int(year)
        if parsed_quality:
            update["quality"] = parsed_quality
        batch.append(UpdateOne({"_id": doc["_id"]}, {"$set": update}))
        processed += 1
        if len(batch) >= batch_size:
            result = await bot.movies.bulk_write(batch, ordered=False)
            updated += result.modified_count
            batch = []
            bot.log.info("v8 normalization progress: %s/%s processed; %s updated.", processed, total, updated)

    if batch:
        result = await bot.movies.bulk_write(batch, ordered=False)
        updated += result.modified_count

    await marker.update_one(
        {"_id": marker_id},
        {"$set": {"completed_at": now, "scanned": total, "updated": updated}},
        upsert=True,
    )
    bot.log.info("Movie title/language normalization v8 complete; scanned %s, updated %s records.", total, updated)
    return updated

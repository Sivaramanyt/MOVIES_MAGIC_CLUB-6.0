import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

NOISE_WORDS = (
    "www", "tamilmv", "tamilblasters", "1tamilblasters", "tamilrockers", "isaimini",
    "hdt", "nf", "webdl", "web", "dl", "webrip", "bluray", "brrip", "hdrip",
    "dvdrip", "dvdscr", "hdcam", "cam", "proper", "hq", "true", "untouched", "untouche",
    "remux", "x264", "x265", "hevc", "avc", "aac", "dd", "dd5", "ddp", "ddp5",
    "atmos", "mkv", "mp4", "avi", "mov", "dual", "audio", "multi", "sample",
    "repack", "rip", "subs", "subtitle", "re", "original", "unrated", "extended",
    "directors", "cut", "version", "movie", "release", "team",
)
LANGUAGES = ("Tamil", "Telugu", "Malayalam", "Kannada", "Hindi", "English", "Bengali", "Marathi")


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", value)
    return clean_text(value)


def canonical_movie_title(value):
    """Turn a release filename into only the movie title.

    Examples such as @TamilMV, [MM], [MS], TamilBlasters, codecs,
    quality tags, sizes and release markers are deliberately discarded.
    """
    text = clean_text(value).lower()
    text = re.sub(r"@[A-Za-z0-9_]+", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    # Parenthesized year / empty release metadata is not part of the title.
    text = re.sub(r"\((?:19|20)?\d{0,4}\)", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b(?:2160p|1080p|720p|480p|360p|4k|uhd|fhd|hd)\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:gb|gib|mb|mib)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:" + "|".join(re.escape(x) for x in NOISE_WORDS) + r")\b", " ", text, flags=re.I)
    text = re.sub(r"\bx\d+\b", " ", text, flags=re.I)
    # A bare x commonly appears in release names like "HDRip x mkv".
    text = re.sub(r"\bx\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"[._+\-]+", " ", text)
    text = re.sub(r"[^a-z0-9\u0B80-\u0BFF\u0900-\u097F\s]", " ", text)
    return clean_text(text)


def display_title(value):
    title = canonical_movie_title(value)
    if not title:
        return "Unknown title"
    # Keep non-Latin scripts unchanged while making normal English titles readable.
    return " ".join(word[:1].upper() + word[1:] for word in title.split())


def parse_metadata(text):
    text = clean_text(text)
    year_match = re.search(r"(?:19|20)\d{2}", text)
    year = int(year_match.group()) if year_match else None
    language = next((x for x in LANGUAGES if re.search(rf"\b{re.escape(x)}\b", text, re.I)), None)
    quality_match = re.search(r"\b(2160p|4K|1080p|720p|480p|360p)\b", text, re.I)
    quality = quality_match.group(1) if quality_match else None
    title = canonical_movie_title(text)
    return display_title(title), year, language, quality


def install(bot):
    # metadata_from_message looks up parse_metadata at call time, so replacing
    # this function fixes both historical imports and future automatic indexing.
    bot.parse_metadata = parse_metadata
    bot.normalize_title = normalize_title


async def migrate_existing_movies(bot):
    """Normalize every existing record and infer missing years when unambiguous.

    v4 intentionally uses a new marker so an earlier incomplete normalization
    can never prevent this cleanup from running.
    """
    marker = bot.db.movie_magic_meta
    marker_id = "title_normalization_v4"
    if await marker.find_one({"_id": marker_id}):
        return 0

    docs = []
    cursor = bot.movies.find(
        {"enabled": True},
        {"_id": 1, "title": 1, "filename": 1, "year": 1, "language": 1, "quality": 1},
    )
    async for doc in cursor:
        docs.append(doc)

    # First determine known years for each canonical movie title.
    known_years = defaultdict(Counter)
    prepared = []
    for doc in docs:
        source = doc.get("filename") or doc.get("title") or ""
        parsed_title, parsed_year, parsed_language, parsed_quality = parse_metadata(source)
        title = parsed_title if parsed_title != "Unknown title" else display_title(doc.get("title") or source)
        year = doc.get("year") or parsed_year
        if year:
            known_years[normalize_title(title)][int(year)] += 1
        prepared.append((doc, title, year, parsed_language, parsed_quality))

    updated = 0
    for doc, title, year, parsed_language, parsed_quality in prepared:
        key = normalize_title(title)
        # If files for this exact title have one unambiguous known year, attach
        # that year to older filenames which omitted it.
        if not year and key in known_years and len(known_years[key]) == 1:
            year = next(iter(known_years[key]))

        update = {
            "title": title,
            "title_normalized": key,
            "title_normalized_at": datetime.now(timezone.utc),
        }
        if year and doc.get("year") != year:
            update["year"] = int(year)
        if (not doc.get("language") or doc.get("language") == "Unknown") and parsed_language:
            update["language"] = parsed_language
        if (not doc.get("quality") or doc.get("quality") == "Unknown") and parsed_quality:
            update["quality"] = parsed_quality

        if any(doc.get(k) != v for k, v in update.items() if k != "title_normalized_at"):
            await bot.movies.update_one({"_id": doc["_id"]}, {"$set": update})
            updated += 1

    await marker.update_one(
        {"_id": marker_id},
        {"$set": {"completed_at": datetime.now(timezone.utc), "scanned": len(docs), "updated": updated}},
        upsert=True,
    )
    return updated

"""Handler-level smoke tests — run without Telegram/MongoDB.

Catches the class of bug that shipped to production: handlers referencing
names that don't exist at module level (NameError only appears at runtime;
py_compile and separate imports never catch it).

Run:  API_ID=1 API_HASH=x BOT_TOKEN=1:x MONGO_URI=mongodb://x STORAGE_CHANNEL_ID=-1001 ADMIN_IDS=1 python3 test_handlers.py
"""

import asyncio
import os
import sys
import types
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot_v2
import file_to_link
import normalization_patch
import verification

normalization_patch.install(bot_v2)

ADMIN_ID = 1
USER_ID = 1001
MOVIE_OID = "0123456789abcdef01234567"
FILE_BYTES = bytes(range(256)) * 4096  # 1 MiB of deterministic pseudo file data


# ---------------------------------------------------------------- fakes ----
class FakePattern:
    def __init__(self, *groups):
        self._groups = groups

    def group(self, i):
        if i > len(self._groups):
            return None  # real match objects return None for unmatched optional groups
        return self._groups[i - 1]


class FakeEvent:
    def __init__(self, sender_id, groups=()):
        self.sender_id = sender_id
        self.pattern_match = FakePattern(*[g.encode() if isinstance(g, str) else g for g in groups])
        self.responses = []
        self.edits = []
        self.answers = []

    async def respond(self, text, buttons=None):
        self.responses.append((text, buttons))
        return types.SimpleNamespace()

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))

    async def answer(self, text=None, alert=False):
        self.answers.append(text)


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return FakeCursor(self._docs[:n])

    async def to_list(self, length=None):
        return self._docs[: length or len(self._docs)]


def _match(query, doc):
    for key, val in query.items():
        if key == "$or":
            if not any(_match(part, doc) for part in val):
                return False
            continue
        dv = doc.get(key)
        if isinstance(dv, list):
            if val not in dv:
                return False
        elif key == "_id":
            if str(dv) != str(val):
                return False
        else:
            if dv != val:
                return False
    return True


class FakeMovies:
    def __init__(self, docs):
        self.docs = docs

    def find(self, q, proj=None):
        return FakeCursor([d for d in self.docs if _match(q, d)])

    async def find_one(self, q, proj=None):
        for d in self.docs:
            if _match(q, d):
                return d
        return None

    async def count_documents(self, q):
        return len([d for d in self.docs if _match(q, d)])


class FakeCol:
    def __init__(self):
        self.docs = {}

    def _key(self, doc):
        return doc.get("_id", doc.get("token"))

    async def find_one(self, q):
        d = self.docs.get(q.get("_id", q.get("token")))
        return dict(d) if d else None

    async def find_one_and_update(self, q, u):
        d = self.docs.get(q.get("_id", q.get("token")))
        if d is None:
            return None
        if q.get("used") == {"$ne": True} and d.get("used") is True:
            return None  # filter {"used": {"$ne": True}} no longer matches
        before = dict(d)
        for k, v in u.get("$set", {}).items():
            d[k] = v
        return before

    async def insert_one(self, doc):
        self.docs[self._key(doc)] = dict(doc)

    async def update_one(self, q, u, upsert=False):
        d = self.docs.get(q["_id"])
        if d is None:
            if not upsert:
                return
            d = {"_id": q["_id"]}
            self.docs[q["_id"]] = d
        for k, v in u.get("$set", {}).items():
            d[k] = v
        for k, v in u.get("$inc", {}).items():
            d[k] = d.get(k, 0) + v

    async def count_documents(self, q):
        if not q:
            return len(self.docs)
        if "verified_until" in q:
            now = q["verified_until"]["$gt"]
            return sum(1 for d in self.docs.values() if d.get("verified_until") and d["verified_until"] > now)
        return len(self.docs)


class FakeDB:
    def __init__(self):
        self.settings = FakeCol()
        self.verifications = FakeCol()
        self.verify_tokens = FakeCol()
        self.file_links = FakeCol()


class FakeTGFile:
    def __init__(self, name="LEO (2023) Tamil 1080p.mkv", size=None, mime="video/x-matroska"):
        self.name = name
        self.size = len(FILE_BYTES) if size is None else size
        self.mime_type = mime


class FakeTGMessage:
    def __init__(self, **file_kwargs):
        self.media = object()  # opaque handle, like a real MessageMediaDocument
        self.file = FakeTGFile(**file_kwargs)


class FakeClient:
    """Mirrors the Telethon client API used by bot_v2 delivery + file_to_link."""

    def __init__(self, **file_kwargs):
        self.msg = FakeTGMessage(**file_kwargs)
        self.sent_files = []
        self.iter_calls = []  # (offset, chunk_size) per Telegram part request

    async def get_messages(self, chat, ids=None):
        return self.msg

    async def send_file(self, entity, file, caption=None, buttons=None, parse_mode=None):
        self.sent_files.append({"entity": entity, "file": file, "caption": caption, "buttons": buttons})
        return types.SimpleNamespace()

    def iter_download(self, media, offset=0, chunk_size=None, **_ignored):
        # async generator, mirroring telethon.iter_download (call it -> async gen).
        # Byte at position i is i % 256 — identical to FILE_BYTES for its length,
        # and valid for ANY file size, so multi-part fetches are testable.
        self.iter_calls.append((offset, chunk_size))
        size = self.msg.file.size
        step = chunk_size or 512 * 1024
        async def gen():
            pos = offset
            while pos < size:
                n = min(step, size - pos)
                yield bytes((pos + i) % 256 for i in range(n))
                pos += n
        return gen()


def expected_bytes(start, length):
    """The procedural file bytes a correct stream must reproduce."""
    return bytes((start + i) % 256 for i in range(length))


def install_fakes(movie_docs, **file_kwargs):
    bot_v2.movies = FakeMovies(movie_docs)
    bot_v2.db = FakeDB()
    bot_v2.client = FakeClient(**file_kwargs)
    return bot_v2.db


# ---------------------------------------------------------------- tests ----
def leo_doc(**over):
    base = {
        "_id": MOVIE_OID, "message_id": 9001, "channel_id": -1001, "enabled": True,
        "title": "Leo", "title_normalized": "leo", "year": 2023,
        "languages": ["Tamil"], "language": "Tamil", "quality": "1080p",
        "filename": "LEO (2023) Tamil 1080p.mkv", "caption": "@Movies_Magic_club",
    }
    base.update(over)
    return base


def test_verify_commands_no_nameerror():
    """The production bug: /verify* handlers crashed with NameError."""
    db = install_fakes([])

    ev = FakeEvent(ADMIN_ID)
    asyncio.run(bot_v2.verify_on(ev))
    assert db.settings.docs.get("verification", {}).get("enabled") is True
    assert "ON" in ev.responses[0][0]

    ev = FakeEvent(ADMIN_ID)
    asyncio.run(bot_v2.verify_status(ev))
    assert "Verification settings" in ev.responses[0][0], ev.responses

    ev = FakeEvent(ADMIN_ID)
    asyncio.run(bot_v2.verify_test(ev))
    assert "self-test" in ev.responses[0][0]

    ev = FakeEvent(ADMIN_ID)
    asyncio.run(bot_v2.verify_reset(ev))
    assert db.verifications.docs[ADMIN_ID]["free_used"] == 0

    ev = FakeEvent(999999)  # non-admin
    asyncio.run(bot_v2.verify_on(ev))
    assert "Admin-only" in ev.responses[0][0]
    print("✅ /verifyon /verifystatus /verifytest /verifyreset run without NameError")


def test_gate_blocks_normal_user_and_allows_after_reset():
    db = install_fakes([leo_doc()])
    os.environ["BASE_URL"] = "https://test.koyeb.app"
    verification.ENV_DEFAULTS["shortlink_api"] = ""  # raw links in tests
    verification.ENV_DEFAULTS["shortlink_url"] = ""

    # normal user at the limit -> gate
    db.verifications.docs[USER_ID] = {
        "_id": USER_ID, "day": verification.today_ist(), "free_used": 3, "verified_until": None,
    }
    ev = FakeEvent(USER_ID, groups=(MOVIE_OID, "0", "0"))
    asyncio.run(bot_v2.choose_quality(ev))
    assert ev.edits, "gate must edit the message"
    gate_text = ev.edits[0][0]
    assert "free limit is over" in gate_text.lower(), gate_text
    assert not bot_v2.client.sent_files, "no files may be sent when gated"
    assert len(db.verify_tokens.docs) == 1, "a one-time token must be created"
    print("✅ normal user at limit -> gate shown, no file sent, token created")

    # after admin reset -> allowed and file sent + counted per file
    db.verifications.docs[USER_ID]["free_used"] = 0
    ev = FakeEvent(USER_ID, groups=(MOVIE_OID, "0", "0"))
    asyncio.run(bot_v2.choose_quality(ev))
    assert bot_v2.client.sent_files, "file must be delivered when allowed"
    assert db.verifications.docs[USER_ID]["free_used"] == 1
    assert "Sent" in ev.edits[-1][0]
    print("✅ after reset -> file delivered and counted per file")

    # verified window -> allowed regardless of counter
    db.verifications.docs[USER_ID]["free_used"] = 99
    db.verifications.docs[USER_ID]["verified_until"] = verification.utcnow() + timedelta(hours=5)
    ev = FakeEvent(USER_ID, groups=(MOVIE_OID, "0", "0"))
    asyncio.run(bot_v2.choose_quality(ev))
    assert "Sent" in ev.edits[-1][0]
    print("✅ verified user bypasses the gate")

    # admin bypass
    ev = FakeEvent(ADMIN_ID, groups=(MOVIE_OID, "0", "0"))
    asyncio.run(bot_v2.choose_quality(ev))
    assert "Sent" in ev.edits[-1][0]
    print("✅ admin bypasses the gate")


def test_parser_part_numbers():
    p = normalization_patch.parse_metadata
    title, year, langs, q = p("Leo (2023) Tamil ProPer TRUE WEB-DL - 1080P HQ - AVC - (DD+5.1 ATMOS - 768KbPs & AAC) - 6.6GB - ESub.Part001.mkv")
    assert (title, year, langs, q) == ("Leo", 2023, ["Tamil"], "1080p"), (title, year, langs, q)
    title, year, langs, q = p("Kalinga_2024_Telugu_720P_HQ_HDRiP_x264_192KbPs_AAC_1_2GB_ESub.mkv")
    assert (title, year, langs, q) == ("Kalinga", 2024, ["Telugu"], "720p"), (title, year, langs, q)
    print("✅ parser: Part001/CD tags stripped, screenshot filenames parse correctly")


# ------------------------------------------------------- file-to-link ----
def test_human_size():
    assert file_to_link.human_size(0) == "0 B"
    assert file_to_link.human_size(500) == "500 B"
    assert file_to_link.human_size(2048) == "2.00 KB"
    assert file_to_link.human_size(int(557.31 * 1024 * 1024)) == "557.31 MB"
    assert file_to_link.human_size(int(2.5 * 1024**3)) == "2.50 GB"
    print("✅ human_size formats like the example (557.31 MB)")


def test_caption_format_matches_example():
    caption = file_to_link.build_file_caption(
        "Agent.Kim.Reactivated.S01E09.1080p.NF.WEB-DL.AAC2.0.H.265-DU.mkv",
        int(557.31 * 1024 * 1024),
        "https://test.koyeb.app/dl/6a7bb27c913c8281a20eca89",
        "https://test.koyeb.app/watch/6a7bb27c913c8281a20eca89",
        bot_username="DDxBypass_Bot",
    )
    expected = (
        "‣ File Name : Agent.Kim.Reactivated.S01E09.1080p.NF.WEB-DL.AAC2.0.H.265-DU.mkv\n\n"
        "‣ File Size : 557.31 MB\n\n"
        "➙ Download : https://test.koyeb.app/dl/6a7bb27c913c8281a20eca89\n\n"
        "➙ Watch Online : https://test.koyeb.app/watch/6a7bb27c913c8281a20eca89\n\n"
        "💡 Tip :- Use IDM (For PC) or 1DM (For Mobile) To Download With Maximum Speed\n\n"
        "CC : @DDxBypass_Bot"
    )
    assert caption == expected, repr(caption)
    print("✅ caption matches the user's example format exactly")


def test_parse_range_header():
    size = 1000
    assert file_to_link.parse_range_header("bytes=0-99", size) == (0, 99)
    assert file_to_link.parse_range_header("bytes=100-", size) == (100, 999)
    assert file_to_link.parse_range_header("bytes=-200", size) == (800, 999)
    assert file_to_link.parse_range_header("bytes=0-99999", size) == (0, 999)  # clamped
    assert file_to_link.parse_range_header("bytes=0-99,200-299", size) == (0, 99)  # first range only
    for bad in ("bytes=999-100", "bytes=1000-", "bytes=-0", "items=0-9", "bytes=abc-"):
        try:
            file_to_link.parse_range_header(bad, size)
            raise AssertionError(f"range {bad!r} must be rejected")
        except ValueError:
            pass
    print("✅ Range header parsing (full / open / suffix / clamped / rejected)")


def test_delivery_attaches_links():
    db = install_fakes([leo_doc()])
    os.environ["BASE_URL"] = "https://test.koyeb.app"
    ev = FakeEvent(USER_ID, groups=(MOVIE_OID, "0", "0"))
    asyncio.run(bot_v2.choose_quality(ev))
    assert len(bot_v2.client.sent_files) == 1, "exactly one file must be delivered"
    sent = bot_v2.client.sent_files[0]
    caption = sent["caption"]
    assert "‣ File Name : LEO (2023) Tamil 1080p.mkv" in caption
    assert "‣ File Size : 1.00 MB" in caption
    assert "➙ Download : https://test.koyeb.app/dl/" in caption
    assert "➙ Watch Online : https://test.koyeb.app/watch/" in caption
    assert "💡 Tip :- Use IDM (For PC) or 1DM (For Mobile) To Download With Maximum Speed" in caption
    assert "CC : @MOVIES_MAGIC_CLUB_bot" in caption
    # same token in both links, and the URL buttons carry the same links
    dl_line = next(l for l in caption.splitlines() if "/dl/" in l)
    watch_line = next(l for l in caption.splitlines() if "/watch/" in l)
    token = dl_line.rsplit("/dl/", 1)[1]
    assert watch_line.endswith(f"/watch/{token}")
    buttons = sent["buttons"][0]
    assert [b.url for b in buttons] == [f"https://test.koyeb.app/dl/{token}", f"https://test.koyeb.app/watch/{token}"]
    # token persisted with a 48h expiry
    assert len(db.file_links.docs) == 1
    link = db.file_links.docs[token]
    assert link["message_id"] == 9001 and link["channel_id"] == -1001
    assert link["expires_at"] > link["created_at"]
    print("✅ delivery: caption + buttons carry /dl and /watch links, token stored with expiry")


def test_web_server_endpoints():
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        db = install_fakes([], name="LEO (2023) Tamil 1080p.mkv")
        now = file_to_link.utcnow()
        await db.file_links.insert_one({
            "token": "goodtoken", "channel_id": -1001, "message_id": 9001,
            "filename": "LEO (2023) Tamil 1080p.mkv", "size": len(FILE_BYTES),
            "mime": "video/x-matroska", "created_at": now,
            "expires_at": now + timedelta(hours=48),
        })
        await db.file_links.insert_one({
            "token": "oldtoken", "channel_id": -1001, "message_id": 9001,
            "filename": "old.mkv", "size": 10, "mime": "video/x-matroska",
            "created_at": now - timedelta(hours=49), "expires_at": now - timedelta(hours=1),
        })
        app = file_to_link.create_app(bot_v2)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/health")
            assert r.status == 200 and await r.text() == "OK"

            r = await client.get("/dl/goodtoken")
            assert r.status == 200, r.status
            assert r.headers["Content-Length"] == str(len(FILE_BYTES))
            assert r.headers["Accept-Ranges"] == "bytes"
            assert "attachment" in r.headers["Content-Disposition"]
            assert await r.read() == FILE_BYTES

            r = await client.get("/dl/goodtoken", headers={"Range": "bytes=100-199"})
            assert r.status == 206, r.status
            assert r.headers["Content-Range"] == f"bytes 100-199/{len(FILE_BYTES)}"
            assert await r.read() == FILE_BYTES[100:200]

            r = await client.get("/stream/goodtoken", headers={"Range": "bytes=-300"})
            assert r.status == 206 and "inline" in r.headers["Content-Disposition"]
            assert await r.read() == FILE_BYTES[-300:]

            r = await client.get("/dl/goodtoken", headers={"Range": "bytes=999999999-"})
            assert r.status == 416

            r = await client.get("/dl/nosuchtoken")
            assert r.status == 404
            r = await client.get("/dl/oldtoken")
            assert r.status == 410  # expired

            # HEAD probe (IDM preflight): headers only, ZERO Telegram downloads
            calls_before = len(bot_v2.client.iter_calls)
            r = await client.head("/dl/goodtoken")
            assert r.status == 200
            assert r.headers["Content-Length"] == str(len(FILE_BYTES))
            assert r.headers["Accept-Ranges"] == "bytes"
            assert "attachment" in r.headers["Content-Disposition"]
            assert len(bot_v2.client.iter_calls) == calls_before, "HEAD must not trigger a download"

            r = await client.get("/watch/goodtoken")
            assert r.status == 200
            html = await r.text()
            assert "/stream/goodtoken" in html and "LEO (2023) Tamil 1080p.mkv" in html
            assert "/dl/goodtoken" in html and "1.00 MB" in html

            # shortlink verification callback (ported from the old health server)
            await db.verify_tokens.insert_one({
                "token": "vtok", "user_id": USER_ID, "created_at": now, "used": False,
            })
            r = await client.get("/verify/vtok")
            assert r.status == 200 and "Verification successful" in await r.text()
            assert db.verifications.docs[USER_ID]["verified_until"] > now
            r = await client.get("/verify/vtok")
            assert "invalid or was already used" in await r.text()
            r = await client.get("/verify/unknown")
            assert "invalid or was already used" in await r.text()

    asyncio.run(main())
    print("✅ web server: /health, /dl (200+206+416+404+410), HEAD probe, /stream, /watch, /verify all pass")


def test_parallel_multipart_streaming():
    """Files bigger than one 512KB part are fetched with parallel part requests
    and reassembled in exact order — the anti-slowdown pipeline."""
    from aiohttp.test_utils import TestClient, TestServer

    big_size = len(FILE_BYTES) + 12345  # 3 parts (512K + 512K + 12345)

    async def main():
        db = install_fakes([], name="big.movie.2024.1080p.mkv", size=big_size)
        now = file_to_link.utcnow()
        await db.file_links.insert_one({
            "token": "bigtoken", "channel_id": -1001, "message_id": 9002,
            "filename": "big.movie.2024.1080p.mkv", "size": big_size,
            "mime": "video/x-matroska", "created_at": now,
            "expires_at": now + timedelta(hours=48),
        })
        app = file_to_link.create_app(bot_v2)
        async with TestClient(TestServer(app)) as client:
            # full file: 3 parallel parts, byte-exact body
            r = await client.get("/dl/bigtoken")
            assert r.status == 200
            assert r.headers["Content-Length"] == str(big_size)
            body = await r.read()
            assert body == expected_bytes(0, big_size), "multi-part body must be byte-exact"
            assert len(bot_v2.client.iter_calls) == 3, bot_v2.client.iter_calls

            # range crossing two parts
            bot_v2.client.iter_calls.clear()
            r = await client.get("/dl/bigtoken", headers={"Range": "bytes=300000-900000"})
            assert r.status == 206
            assert r.headers["Content-Range"] == f"bytes 300000-900000/{big_size}"
            assert await r.read() == expected_bytes(300000, 600001)
            assert len(bot_v2.client.iter_calls) == 2

            # tiny unaligned range: exactly one part fetch, exact bytes
            bot_v2.client.iter_calls.clear()
            r = await client.get("/dl/bigtoken", headers={"Range": "bytes=13-357"})
            assert r.status == 206
            assert await r.read() == expected_bytes(13, 345)
            assert len(bot_v2.client.iter_calls) == 1

    asyncio.run(main())
    print("✅ parallel streaming: 3-part file byte-exact, cross-part ranges exact, HEAD-free probes")


if __name__ == "__main__":
    test_verify_commands_no_nameerror()
    test_gate_blocks_normal_user_and_allows_after_reset()
    test_parser_part_numbers()
    test_human_size()
    test_caption_format_matches_example()
    test_parse_range_header()
    test_delivery_attaches_links()
    test_web_server_endpoints()
    test_parallel_multipart_streaming()
    print("\nALL HANDLER SMOKE TESTS PASSED")

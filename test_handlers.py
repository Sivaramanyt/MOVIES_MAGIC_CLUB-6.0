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
import normalization_patch
import verification

normalization_patch.install(bot_v2)

ADMIN_ID = 1
USER_ID = 1001
MOVIE_OID = "0123456789abcdef01234567"


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


class FakeClient:
    def __init__(self):
        self.forwarded = []

    async def forward_messages(self, *a, **k):
        self.forwarded.append((a, k))


def install_fakes(movie_docs):
    bot_v2.movies = FakeMovies(movie_docs)
    bot_v2.db = FakeDB()
    bot_v2.client = FakeClient()
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
    assert not bot_v2.client.forwarded, "no files may be sent when gated"
    assert len(db.verify_tokens.docs) == 1, "a one-time token must be created"
    print("✅ normal user at limit -> gate shown, no file sent, token created")

    # after admin reset -> allowed and file sent + counted per file
    db.verifications.docs[USER_ID]["free_used"] = 0
    ev = FakeEvent(USER_ID, groups=(MOVIE_OID, "0", "0"))
    asyncio.run(bot_v2.choose_quality(ev))
    assert bot_v2.client.forwarded, "file must be forwarded when allowed"
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


if __name__ == "__main__":
    test_verify_commands_no_nameerror()
    test_gate_blocks_normal_user_and_allows_after_reset()
    test_parser_part_numbers()
    print("\nALL HANDLER SMOKE TESTS PASSED")

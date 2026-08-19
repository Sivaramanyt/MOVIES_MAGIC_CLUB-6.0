"""Public HTTP server: Koyeb health checks + shortlink verification callback.

Runs in a daemon thread, independent of the Telegram bot's event loop.
Health endpoints must stay available on PORT (8000) at all times.

GET /verify/<token> completes a user's shortlink verification: the one-time
token is consumed and the user's `verified_until` is set to now + valid_hours
(read from db.settings {_id: "verification"}, default VERIFY_VALID_HOURS=24).
MongoDB access here uses a separate SYNCHRONOUS pymongo client (the bot's
motor client belongs to the asyncio loop and must not be shared across threads).
"""

import os
import re
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "8000"))
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "movie_magic_club")
VERIFY_VALID_HOURS_DEFAULT = int(os.getenv("VERIFY_VALID_HOURS", "24"))
TOKEN_TTL_HOURS = int(os.getenv("VERIFY_TOKEN_HOURS", "24"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "MOVIES_MAGIC_CLUB_bot").lstrip("@")

_mongo_client = None


def utcnow():
    """Naive UTC — matches pymongo's round-trip so comparisons stay consistent."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_db():
    global _mongo_client
    if _mongo_client is None:
        if not MONGO_URI:
            return None
        from pymongo import MongoClient
        _mongo_client = MongoClient(MONGO_URI)
    return _mongo_client[MONGO_DB]


def process_verify_token(token, db, now=None):
    """Consume a one-time token and grant its user unlimited access.

    Returns (ok, detail): ok=True → detail is the verified_until datetime;
    ok=False → detail is a human-readable failure reason.
    """
    now = now or utcnow()
    token_doc = db.verify_tokens.find_one_and_update(
        {"token": token, "used": {"$ne": True}},
        {"$set": {"used": True, "used_at": now}},
    )
    if not token_doc:
        return False, "This verification link is invalid or was already used."
    created = token_doc.get("created_at")
    if created and (now - created) > timedelta(hours=TOKEN_TTL_HOURS):
        return False, "This verification link has expired."
    settings = db.settings.find_one({"_id": "verification"}) or {}
    hours = int(settings.get("valid_hours", VERIFY_VALID_HOURS_DEFAULT) or VERIFY_VALID_HOURS_DEFAULT)
    verified_until = now + timedelta(hours=hours)
    db.verifications.update_one(
        {"_id": token_doc["user_id"]},
        {
            "$set": {"verified_until": verified_until, "free_used": 0, "updated_at": now},
            "$setOnInsert": {"day": ""},
        },
        upsert=True,
    )
    return True, verified_until


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


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health", "/healthz"):
            self._respond(200, b"OK", "text/plain; charset=utf-8")
            return
        match = re.fullmatch(r"/verify/([A-Za-z0-9_\-]+)", path)
        if match:
            self._handle_verify(match.group(1))
            return
        self._respond(404, b"Not found", "text/plain; charset=utf-8")

    def _handle_verify(self, token):
        try:
            db = get_db()
            if db is None:
                status, html = 500, page(
                    "Error", "⚠️ Service unavailable", "Verification service is not ready. Please try again shortly."
                )
            else:
                ok, detail = process_verify_token(token, db)
                if ok:
                    until_ist = detail + timedelta(hours=5, minutes=30)
                    status, html = 200, page(
                        "Verification successful",
                        "✅ Verification successful!",
                        "You now have <b>UNLIMITED movies for 24 hours</b>. 🎉<br><br>"
                        f"Valid until <b>{until_ist:%d %b %Y, %I:%M %p} IST</b>.<br>"
                        "Return to Telegram and choose your movie again.",
                        show_bot_button=True,
                    )
                else:
                    status, html = 200, page(
                        "Verification failed",
                        "❌ Link not valid",
                        escape(detail) + "<br><br>Go back to the bot and tap the verification button again to get a fresh link.",
                        show_bot_button=True,
                    )
        except Exception:
            status, html = 500, page("Error", "⚠️ Error", "Something went wrong. Please try again.")
        self._respond(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def run_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

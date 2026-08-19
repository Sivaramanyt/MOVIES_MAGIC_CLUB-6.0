"""Shortlink verification gate for file delivery.

Free tier: each user gets FREE_LIMIT free file deliveries per day (default 3).
The next delivery shows a verification gate instead: the user completes one
shortlink (monetized) and gets UNLIMITED deliveries for VERIFY_VALID_HOURS
(default 24). When the verified window expires, the user returns to the free
tier with a fresh free allowance. The free counter also resets at midnight IST.

Runtime-tunable settings live in db.settings {_id: "verification"} and can be
changed with the admin commands (/verifyon /verifyoff /verifylimit /verifyhours).
Env vars provide the defaults:

  VERIFICATION_ENABLED=true|false
  FREE_LIMIT=3
  VERIFY_VALID_HOURS=24
  VERIFY_TOKEN_HOURS=24        (how long a verification link stays valid)
  SHORTLINK_API / SHORTLINK_URL (shortlink service credentials, e.g. arolinks)
  BASE_URL                     (public app URL; falls back to KOYEB_PUBLIC_DOMAIN)
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("movie-verification")

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_TTL = timedelta(hours=int(os.getenv("VERIFY_TOKEN_HOURS", "24")))

ENV_DEFAULTS = {
    "enabled": os.getenv("VERIFICATION_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"),
    "free_limit": int(os.getenv("FREE_LIMIT", "3")),
    "valid_hours": int(os.getenv("VERIFY_VALID_HOURS", "24")),
    "shortlink_api": os.getenv("SHORTLINK_API", "").strip(),
    "shortlink_url": os.getenv("SHORTLINK_URL", "").strip(),
}


def utcnow():
    """Naive UTC — matches what pymongo returns, so comparisons never mix tz."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


def public_base_url():
    base = os.getenv("BASE_URL", "").strip().rstrip("/")
    if not base:
        domain = os.getenv("KOYEB_PUBLIC_DOMAIN", "").strip()
        if domain:
            base = f"https://{domain}"
    return base


async def get_settings(bot):
    doc = await bot.db.settings.find_one({"_id": "verification"}) or {}
    out = dict(ENV_DEFAULTS)
    for key in ("enabled", "free_limit", "valid_hours", "shortlink_api", "shortlink_url"):
        if doc.get(key) not in (None, ""):
            out[key] = doc[key]
    out["enabled"] = bool(out["enabled"])
    out["free_limit"] = int(out["free_limit"])
    out["valid_hours"] = int(out["valid_hours"])
    return out


async def set_setting(bot, key, value):
    await bot.db.settings.update_one({"_id": "verification"}, {"$set": {key: value}}, upsert=True)


async def get_state(bot, user_id):
    """Load verification state, applying the daily reset and window-expiry reset."""
    col = bot.db.verifications
    now = utcnow()
    doc = await col.find_one({"_id": user_id})
    if not doc:
        doc = {"_id": user_id, "day": today_ist(), "free_used": 0, "verified_until": None}
        await col.insert_one({**doc, "updated_at": now})
        return doc
    changed = {}
    if doc.get("day") != today_ist():
        # new day → fresh free allowance; a live verified window survives midnight
        changed["day"] = today_ist()
        changed["free_used"] = 0
    vu = doc.get("verified_until")
    if vu and now >= vu:
        # verified window expired → back to free tier with a fresh allowance
        changed["verified_until"] = None
        changed["free_used"] = 0
    if changed:
        changed["updated_at"] = now
        await col.update_one({"_id": user_id}, {"$set": changed})
        doc.update(changed)
    return doc


def is_blocked(settings, state, now=None):
    """Pure decision: should this user hit the verification gate right now?"""
    now = now or utcnow()
    if not settings["enabled"]:
        return False
    vu = state.get("verified_until")
    if vu and now < vu:
        return False
    return int(state.get("free_used", 0)) >= settings["free_limit"]


def _shorten_sync(original_url, api_key, service):
    """Universal shortlink API call (arolinks/gplinks/shrinkme style)."""
    endpoint = service if service.startswith("http") else f"https://{service}"
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/api"):
        endpoint += "/api"
    for method, key in (("GET", "api"), ("POST", "api"), ("GET", "key")):
        try:
            if method == "GET":
                resp = requests.get(endpoint, params={key: api_key, "url": original_url}, timeout=15)
            else:
                resp = requests.post(endpoint, data={key: api_key, "url": original_url}, timeout=15)
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except ValueError:
                text = resp.text.strip()
                if text.startswith("http"):
                    return text
                continue
            for field in ("shortenedUrl", "short_url", "shortUrl", "url", "link"):
                link = data.get(field)
                if isinstance(link, str) and link.startswith("http"):
                    return link
        except Exception as exc:
            log.warning("Shortlink %s attempt failed: %s", method, exc)
    log.error("All shortlink API formats failed; using the raw verification link")
    return original_url


async def build_verification_link(bot, user_id, settings):
    """Create a one-time token and return the (shortened) verification URL."""
    base = public_base_url()
    if not base:
        return None
    token = secrets.token_urlsafe(16)
    await bot.db.verify_tokens.insert_one(
        {"token": token, "user_id": user_id, "created_at": utcnow(), "used": False}
    )
    verify_url = f"{base}/verify/{token}"
    api_key = settings.get("shortlink_api")
    service = settings.get("shortlink_url")
    if not api_key or not service:
        log.warning("SHORTLINK_API/SHORTLINK_URL not configured; using the raw verification link")
        return verify_url
    return await asyncio.to_thread(_shorten_sync, verify_url, api_key, service)


async def check_access(bot, user_id):
    """Return (allowed, context). When blocked, context has link + free_limit."""
    try:
        settings = await get_settings(bot)
        state = await get_state(bot, user_id)
        if not is_blocked(settings, state):
            return True, None
        log.info(
            "Gate: blocking user %s (free_used=%s, limit=%s) — issuing verification link",
            user_id, state.get("free_used", 0), settings["free_limit"],
        )
        link = await build_verification_link(bot, user_id, settings)
        if not link:
            log.error("User %s hit the gate but no public BASE_URL is configured; failing open", user_id)
            return True, None
        return False, {"link": link, "free_limit": settings["free_limit"], "valid_hours": settings["valid_hours"]}
    except Exception:
        log.exception("Verification check failed; failing open")
        return True, None


async def record_delivery(bot, user_id, count=1):
    """Count delivered FILES against the daily free allowance."""
    if count <= 0:
        return
    try:
        await get_state(bot, user_id)  # ensures doc exists and resets are applied
        await bot.db.verifications.update_one(
            {"_id": user_id},
            {"$inc": {"free_used": int(count)}, "$set": {"updated_at": utcnow()}},
        )
    except Exception:
        log.exception("Could not record delivery for user %s", user_id)


async def status_text(bot, user_id):
    settings = await get_settings(bot)
    state = await get_state(bot, user_id)
    if not settings["enabled"]:
        return "✅ Verification is currently disabled — enjoy unlimited movies!"
    vu = state.get("verified_until")
    if vu and utcnow() < vu:
        vu_ist = vu + timedelta(hours=5, minutes=30)
        return (
            "✅ **Verification active!**\n\n"
            f"Unlimited movies until **{vu_ist:%d %b %Y, %I:%M %p} IST**.\n"
            "Send any movie name to continue."
        )
    remaining = max(0, settings["free_limit"] - int(state.get("free_used", 0)))
    return (
        f"🎬 You have **{remaining}** of **{settings['free_limit']}** free files left today.\n\n"
        "Send any movie name to continue."
    )

"""
bot_health.py - lightweight health + daily activity tracker for /stats.

Safety rules of this module:
  * every record_* function is sync, in-memory and never raises
  * the database is touched only by flush() (once a minute, one update_one)
  * no database/plugin import at module level (no circular imports)
"""
import os
import time
import asyncio
import logging
import datetime
import functools
from collections import deque

import psutil
import pytz

from info import ADMINS, MULTIPLE_DB

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ----------------------------- settings ---------------------------------
RAM_LIMIT_MB = _env_int("RAM_LIMIT_MB", 512)   # Render free plan = 512 MB
DB_LIMIT_BYTES = 512 * 1024 * 1024             # MongoDB free cluster
RAM_YELLOW, RAM_RED = 70, 85                   # % of RAM_LIMIT_MB
SPEED_YELLOW, SPEED_RED = 3.0, 5.0             # avg search seconds
DB_WARN_PCT = 90                               # % of DB used
UPTIME_WARN = 24 * 3600                        # seconds
SPEED_WINDOW = 900                             # only last 15 min of searches count
ALERT_AFTER = 300                              # red must last 5 min before PM alert
ALERT_COOLDOWN = 6 * 3600                      # same alert at most once / 6h
DB_ALERT_COOLDOWN = 12 * 3600
FLUSH_EVERY = 60                               # seconds
STATS_MIN_AGE = 10                             # seconds, DB numbers cache
DAILY_KEEP_DAYS = 30

LIGHT = {
    "green": "🟢 ʙᴏᴛ ʜᴇᴀʟᴛʜʏ",
    "yellow": "🟡 ᴡᴀᴛᴄʜ ᴄʟᴏsᴇʟʏ",
    "red": "🔴 ʀᴇsᴛᴀʀᴛ ɴᴇᴇᴅᴇᴅ",
}

# ------------------------------ state -----------------------------------
_counts = {"found": 0, "missed": 0, "downloads": 0, "new_users": 0, "files_added": 0}
_active = set()
_speed = deque(maxlen=200)      # (monotonic_time, seconds)
_flood_until = 0.0
_index_ready = False
_heavy = {"ts": 0.0, "data": None}
_heavy_lock = None
_flood_patched = False
_proc = psutil.Process(os.getpid())


# --------------------------- record helpers -----------------------------
def perf():
    """Timer helper so other modules need no extra import."""
    return time.perf_counter()


def _touch(user_id):
    try:
        if user_id:
            _active.add(int(user_id))
    except (TypeError, ValueError):
        pass


def record_active(user_id=None):
    _touch(user_id)


def record_found():
    _counts["found"] += 1


def record_missed():
    _counts["missed"] += 1


def record_download(user_id=None):
    _counts["downloads"] += 1
    _touch(user_id)


def record_new_user():
    _counts["new_users"] += 1


def record_file_added():
    _counts["files_added"] += 1


def record_speed(seconds):
    try:
        _speed.append((time.monotonic(), float(seconds)))
    except (TypeError, ValueError):
        pass


def record_flood(seconds):
    global _flood_until
    try:
        secs = int(seconds)
    except (TypeError, ValueError):
        return
    if secs > 0:
        _flood_until = max(_flood_until, time.time() + secs)


def flood_left():
    return max(0, int(_flood_until - time.time()))


def avg_speed():
    """Average search time of the last 15 minutes (None if too few samples)."""
    cutoff = time.monotonic() - SPEED_WINDOW
    vals = [s for t, s in list(_speed) if t >= cutoff]
    if len(vals) < 3:
        return None
    return sum(vals) / len(vals)


def install_flood_watch():
    """Remember Pyrogram FloodWait errors (the error is still raised as before)."""
    global _flood_patched
    if _flood_patched:
        return
    try:
        from pyrogram import Client
        from pyrogram.errors import FloodWait
    except Exception as e:  # pyrogram missing -> just skip
        logger.warning("flood watch not installed: %s", e)
        return
    original = Client.invoke

    @functools.wraps(original)
    async def invoke(self, *args, **kwargs):
        try:
            return await original(self, *args, **kwargs)
        except FloodWait as e:
            record_flood(getattr(e, "value", 0))
            raise

    Client.invoke = invoke
    _flood_patched = True


# ------------------------------ database --------------------------------
def _today():
    return datetime.datetime.now(IST).strftime("%Y-%m-%d")


def _col():
    from database.users_chats_db import db
    return db.db.bot_daily_stats


async def _ensure_index():
    global _index_ready
    if _index_ready:
        return
    _index_ready = True
    try:
        await _col().create_index("ts", expireAfterSeconds=DAILY_KEEP_DAYS * 86400)
    except Exception as e:
        logger.error("health index error: %s", e)


async def flush():
    """Write pending counters to today's document (one small update)."""
    global _active
    inc = {k: v for k, v in _counts.items() if v}
    active = list(_active)
    if not inc and not active:
        return
    for k in _counts:
        _counts[k] = 0
    _active = set()
    update = {"$setOnInsert": {"ts": datetime.datetime.now(datetime.timezone.utc)}}
    if inc:
        update["$inc"] = inc
    if active:
        update["$addToSet"] = {"active": {"$each": active}}
    try:
        await _ensure_index()
        await _col().update_one({"_id": _today()}, update, upsert=True)
    except Exception as e:
        # keep the numbers, try again next minute
        for k, v in inc.items():
            _counts[k] += v
        _active.update(active)
        logger.error("health flush error: %s", e)


async def get_today():
    await flush()
    data = {"found": 0, "missed": 0, "downloads": 0, "new_users": 0,
            "files_added": 0, "active": 0}
    try:
        doc = await _col().find_one({"_id": _today()})
        if doc:
            for k in ("found", "missed", "downloads", "new_users", "files_added"):
                data[k] = int(doc.get(k, 0) or 0)
            data["active"] = len(doc.get("active", []) or [])
    except Exception as e:
        logger.error("health get_today error: %s", e)
    return data


async def _cluster_size(mongo_client):
    total = 0
    for name in await mongo_client.list_database_names():
        if name in ("admin", "local"):
            continue
        stats = await mongo_client[name].command("dbStats")
        total += stats["storageSize"] + stats["indexSize"]
    return total


async def _collect_db_info():
    from database.users_chats_db import db
    from database.ia_filterdb import Media, Media2, client, client2
    info = {
        "users": await db.total_users_count(),
        "groups": await db.total_chat_count(),
        "premium": await db.all_premium_users(),
        "files1": await Media.count_documents(),
        "used1": await _cluster_size(client),
        "files2": 0,
        "used2": 0,
    }
    if MULTIPLE_DB:
        info["files2"] = await Media2.count_documents()
        info["used2"] = await _cluster_size(client2)
    return info


async def get_db_info():
    """DB numbers (users, files, storage). Cached for a few seconds."""
    global _heavy_lock
    if _heavy_lock is None:
        _heavy_lock = asyncio.Lock()
    async with _heavy_lock:
        if _heavy["data"] is not None and time.time() - _heavy["ts"] < STATS_MIN_AGE:
            return _heavy["data"]
        data = await _collect_db_info()
        _heavy["ts"] = time.time()
        _heavy["data"] = data
        return data


# ------------------------------ health ----------------------------------
def ram_info():
    mb = _proc.memory_info().rss / 1024 / 1024
    pct = (mb / RAM_LIMIT_MB * 100) if RAM_LIMIT_MB > 0 else 0.0
    return mb, pct


def db_used_pct(info):
    pct1 = info["used1"] / DB_LIMIT_BYTES * 100
    if not MULTIPLE_DB:
        return pct1
    pct2 = info["used2"] / DB_LIMIT_BYTES * 100
    return min(pct1, pct2)   # bot can still save while one DB has room


def evaluate(ram_pct, speed, flood, db_pct, uptime_s):
    """Return (level, flags). level: green / yellow / red."""
    flags = {}
    if ram_pct >= RAM_RED:
        flags["ram"] = "red"
    elif ram_pct >= RAM_YELLOW:
        flags["ram"] = "yellow"
    if speed is not None:
        if speed >= SPEED_RED:
            flags["speed"] = "red"
        elif speed >= SPEED_YELLOW:
            flags["speed"] = "yellow"
    if flood > 0:
        flags["flood"] = "yellow"
    if db_pct >= DB_WARN_PCT:
        flags["db"] = "yellow"
    if uptime_s >= UPTIME_WARN:
        flags["uptime"] = "yellow"
    if "red" in flags.values():
        level = "red"
    elif flags:
        level = "yellow"
    else:
        level = "green"
    return level, flags


# ------------------------------ text ------------------------------------
def _duration(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _short(sec):
    return " ".join(_duration(sec).split()[:2])


def _mb(num_bytes):
    return f"{num_bytes / 1024 / 1024:.2f} MB"


def build_text(info, today, uptime_s):
    """Return (html_text, level) for the /stats message."""
    mb, ram_pct = ram_info()
    speed = avg_speed()
    flood = flood_left()
    db_pct = db_used_pct(info)
    level, flags = evaluate(ram_pct, speed, flood, db_pct, uptime_s)

    def warn(name):
        return " ⚠️" if name in flags else ""

    found, missed = today["found"], today["missed"]
    total = found + missed
    hit = f"{round(found * 100 / total)}%" if total else "N/A"
    speed_txt = f"{speed:.2f}s (avg)" if speed is not None else "N/A"
    flood_txt = f"🚫 {_short(flood)} left" if flood else "✅ ɴᴏɴᴇ"
    limit_txt = f"{DB_LIMIT_BYTES // 1024 // 1024} MB"

    if MULTIPLE_DB:
        files_lines = (
            f"» ᴛᴏᴛᴀʟ ꜰɪʟᴇs - {info['files1'] + info['files2']} ({info['files1']} + {info['files2']})\n"
            f"» ᴀᴅᴅᴇᴅ ᴛᴏᴅᴀʏ - {today['files_added']}\n"
            f"» sᴛᴏʀᴀɢᴇ 𝟷 - {_mb(info['used1'])} / {limit_txt} ({info['used1'] / DB_LIMIT_BYTES * 100:.0f}%)\n"
            f"» sᴛᴏʀᴀɢᴇ 𝟸 - {_mb(info['used2'])} / {limit_txt} ({info['used2'] / DB_LIMIT_BYTES * 100:.0f}%){warn('db')}"
        )
    else:
        files_lines = (
            f"» ᴛᴏᴛᴀʟ ꜰɪʟᴇs - {info['files1']}\n"
            f"» ᴀᴅᴅᴇᴅ ᴛᴏᴅᴀʏ - {today['files_added']}\n"
            f"» sᴛᴏʀᴀɢᴇ - {_mb(info['used1'])} / {limit_txt} ({db_pct:.0f}%){warn('db')}"
        )

    banned = 0
    try:
        from utils import temp
        banned = len(temp.BANNED_USERS)
    except Exception:
        pass

    text = (
        f"<b>{LIGHT[level]}\n\n"
        f"🗃 ᴜsᴇʀs ᴅᴀᴛᴀʙᴀsᴇ 🗃\n\n"
        f"» ᴛᴏᴛᴀʟ ᴜsᴇʀs - {info['users']}\n"
        f"» ᴀᴄᴛɪᴠᴇ ᴛᴏᴅᴀʏ - {today['active']}\n"
        f"» ɴᴇᴡ ᴛᴏᴅᴀʏ - {today['new_users']}\n"
        f"» ᴛᴏᴛᴀʟ ɢʀᴏᴜᴘs - {info['groups']}\n"
        f"» ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs - {info['premium']}\n"
        f"» ʙᴀɴɴᴇᴅ ᴜsᴇʀs - {banned}\n\n"
        f"📤 ꜰɪʟᴇs ᴅᴀᴛᴀʙᴀsᴇ 📤\n\n"
        f"{files_lines}\n\n"
        f"📊 ᴛᴏᴅᴀʏ'ꜱ ᴀᴄᴛɪᴠɪᴛʏ 📊\n\n"
        f"» sᴇᴀʀᴄʜᴇs - {total} (✅ {found} | ❌ {missed})\n"
        f"» ʜɪᴛ ʀᴀᴛᴇ - {hit}\n"
        f"» ᴅᴏᴡɴʟᴏᴀᴅs - {today['downloads']}\n\n"
        f"🤖 ʙᴏᴛ ʜᴇᴀʟᴛʜ 🤖\n\n"
        f"» ᴜᴘᴛɪᴍᴇ - {_duration(uptime_s)}{warn('uptime')}\n"
        f"» ʀᴀᴍ - {mb:.0f} MB / {RAM_LIMIT_MB} MB ({ram_pct:.0f}%){warn('ram')}\n"
        f"» ᴄᴘᴜ - {psutil.cpu_percent():.0f}%\n"
        f"» sᴇᴀʀᴄʜ sᴘᴇᴇᴅ - {speed_txt}{warn('speed')}\n"
        f"» ꜰʟᴏᴏᴅ ᴡᴀɪᴛ - {flood_txt}</b>"
    )
    return text, level


# ----------------------------- monitor ----------------------------------
def _cooldown_ok(last, key, now, seconds):
    return now - last.get(key, 0) >= seconds


async def _alert(client, text):
    sent = 0
    for admin in ADMINS:
        if not isinstance(admin, int):
            continue
        try:
            await client.send_message(admin, text)
            sent += 1
        except Exception as e:
            logger.warning("health alert to %s failed: %s", admin, e)
    return sent


async def _check_db(client, last):
    info = await get_db_info()
    pct = db_used_pct(info)
    now = time.time()
    if pct >= DB_WARN_PCT and _cooldown_ok(last, "db", now, DB_ALERT_COOLDOWN):
        last["db"] = now
        await _alert(
            client,
            f"<b>⚠️ ᴅᴀᴛᴀʙᴀsᴇ ᴀʟᴍᴏsᴛ ꜰᴜʟʟ\n\n» ᴜsᴇᴅ - {pct:.0f}%\n"
            f"» New files may stop saving soon.\n"
            f"» Delete old files or add a new database.</b>",
        )


async def monitor(client):
    """Background loop: flush counters every minute + alert admins when needed."""
    await asyncio.sleep(30)
    bad_since = None
    last = {}
    ticks = 0
    while True:
        try:
            await flush()
            ticks += 1
            now = time.time()
            mb, ram_pct = ram_info()
            speed = avg_speed()
            reasons = []
            if ram_pct >= RAM_RED:
                reasons.append(f"» ʀᴀᴍ - {mb:.0f} MB / {RAM_LIMIT_MB} MB ({ram_pct:.0f}%)")
            if speed is not None and speed >= SPEED_RED:
                reasons.append(f"» sᴇᴀʀᴄʜ sᴘᴇᴇᴅ - {speed:.1f}s (avg)")
            if reasons:
                if bad_since is None:
                    bad_since = now
                if now - bad_since >= ALERT_AFTER and _cooldown_ok(last, "restart", now, ALERT_COOLDOWN):
                    last["restart"] = now
                    await _alert(
                        client,
                        "<b>🔴 ʀᴇsᴛᴀʀᴛ ɴᴇᴇᴅᴇᴅ\n\n" + "\n".join(reasons)
                        + "\n\n» Open /stats and press ♻️ Restart.</b>",
                    )
            else:
                bad_since = None
            if ticks % 30 == 1:   # first run + roughly every 30 minutes
                await _check_db(client, last)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("health monitor error: %s", e)
        await asyncio.sleep(FLUSH_EVERY)


install_flood_watch()

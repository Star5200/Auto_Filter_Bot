"""
Lens plugin
-----------
Sirf LENS_CHANNELS mein bheji gayi files par kaam karta hai:
  1. File ke pehle kuch MB scan karta hai (MediaInfo) -> audio languages nikalta hai
  2. Wahi file bot khud wapis bhejta hai (file_id se copy, re-upload nahi hota -> instant)
     caption ke neeche quote mein:  🔊 #Korean, #English
  3. (optional) original message delete kar deta hai

Render env vars:
  LENS_CHANNELS         = -100xxxxxxxxxx            (space se alag karke kai chat bhi de sakte ho)
  LENS_DELETE_ORIGINAL  = False                     (True = original file delete karke sirf bot wali rakhe)
  LENS_SCAN_CHUNKS      = 6                         (shuru ke kitne MB scan karne hain, 1 chunk = 1 MB)
  LENS_TAIL_CHUNKS      = 8                         (language na mile toh aakhri kitne MB bhi scan karne hain)
  LENS_SHOW_UNKNOWN     = True                      (kuch bhi detect na ho toh #Unknown dikhao)

Note: LENS_CHANNELS wali chat ko CHANNELS (indexing) mein mat daalna.
"""
import asyncio
import heapq
import html
import itertools
import logging
import os
import re
import tempfile
import time
import uuid
from collections import defaultdict, deque

import aiofiles
from pymediainfo import MediaInfo
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait

from utils import detect_langs

logger = logging.getLogger(__name__)


def _env_list(name):
    out = []
    for x in os.environ.get(name, "").replace(",", " ").split():
        out.append(int(x) if re.fullmatch(r"-?\d+", x) else x)
    return out


LENS_CHANNELS = _env_list("LENS_CHANNELS")
DELETE_ORIGINAL = os.environ.get("LENS_DELETE_ORIGINAL", "False").strip().lower() in ("true", "1", "yes")
SCAN_CHUNKS = max(2, int(os.environ.get("LENS_SCAN_CHUNKS", "6") or 6))
SCAN_TIMEOUT = 45

# ISO 639-1 / 639-2 codes (MediaInfo inhi mein language deta hai)
_LANGS = {
    "Hindi": "hi hin", "English": "en eng", "Tamil": "ta tam", "Telugu": "te tel",
    "Malayalam": "ml mal", "Kannada": "kn kan", "Bengali": "bn ben", "Marathi": "mr mar",
    "Punjabi": "pa pan pun", "Gujarati": "gu guj", "Urdu": "ur urd", "Odia": "or ori",
    "Assamese": "as asm", "Bhojpuri": "bho", "Nepali": "ne nep",
    "Korean": "ko kor", "Japanese": "ja jpn jap", "Chinese": "zh zho chi cmn yue",
    "Thai": "th tha", "Indonesian": "id ind", "Malay": "ms msa may", "Vietnamese": "vi vie",
    "Filipino": "fil tl tgl", "Spanish": "es spa", "French": "fr fra fre",
    "German": "de deu ger", "Italian": "it ita", "Portuguese": "pt por", "Russian": "ru rus",
    "Turkish": "tr tur", "Arabic": "ar ara", "Persian": "fa fas per", "Hebrew": "he heb",
    "Polish": "pl pol", "Dutch": "nl nld dut", "Swedish": "sv swe",
}
LANG_BY_CODE = {}
for _name, _codes in _LANGS.items():
    LANG_BY_CODE[_name.lower()] = _name
    for _c in _codes.split():
        LANG_BY_CODE[_c] = _name

_flood_until = 0.0
_own_ids = deque(maxlen=1000)
_queues = defaultdict(list)   # chat_id -> heap of (message.id, n, message)
_workers = {}
_counter = itertools.count()
COLLECT_DELAY = 1.5


def _lang_of_track(track):
    """MediaInfo audio track -> language name (ya None agar pata na chale)."""
    cands = [str(track.language or "")]
    cands += [str(o) for o in (getattr(track, "other_language", None) or [])]
    for c in cands:
        c = re.split(r"[-_ ]", c.strip().lower())[0]
        if c in LANG_BY_CODE:
            return LANG_BY_CODE[c]
    # language "und" ho toh track ka title try karo (e.g. "Hindi 5.1")
    found = [n for n in detect_langs(track.title or "") if n != "Dual Audio"]
    if found:
        return found[0]
    other = getattr(track, "other_language", None)
    if other and str(track.language or "und").lower() != "und":
        name = re.sub(r"[^A-Za-z]", "", str(other[0]))
        if name:
            return name.capitalize()
    return None


CHUNK = 1024 * 1024
TAIL_CHUNKS = max(2, int(os.environ.get("LENS_TAIL_CHUNKS", "8") or 8))
SHOW_UNKNOWN = os.environ.get("LENS_SHOW_UNKNOWN", "True").strip().lower() in ("true", "1", "yes")

# file name / caption ke liye: sirf poore naam aur safe 3-letter codes (2-letter codes se galat match hota hai)
_TEXT_TOKENS = {n.lower(): n for n in _LANGS}
for _name, _codes in _LANGS.items():
    for _c in _codes.split():
        if len(_c) == 3 and _c not in ("may", "per", "ind", "ita", "pol", "ara", "fil", "spa", "dut", "swe", "ori", "asm", "mar", "ben"):
            _TEXT_TOKENS[_c] = _name
_TEXT_TOKENS.update({"cht": "Chinese", "chs": "Chinese", "mandarin": "Chinese", "cantonese": "Chinese",
                     "tagalog": "Filipino", "farsi": "Persian", "panjabi": "Punjabi", "oriya": "Odia"})


def detect_in_text(text):
    """File name / caption mein se languages (aane ke order mein)."""
    words = re.findall(r"[a-z]+", str(text or "").lower())
    out = []
    for w in words:
        n = _TEXT_TOKENS.get(w)
        if n and n not in out:
            out.append(n)
    return out


def _read_langs(info):
    """-> (languages, unresolved_audio_tracks, audio_track_count)"""
    langs, unresolved, count = [], 0, 0
    for t in info.tracks:
        if (t.track_type or "").lower() != "audio":
            continue
        count += 1
        name = _lang_of_track(t)
        if name:
            if name not in langs:
                langs.append(name)
        else:
            unresolved += 1
    return langs, unresolved, count


async def scan_audio_languages(client, message, media):
    """
    Pehle file ke shuru ke SCAN_CHUNKS MB scan karta hai (MKV ka header yahin hota hai).
    Agar audio track na mile ya kisi track ki language 'und' ho (MP4 ka moov aksar file ke END mein
    hota hai), toh file ke aakhri TAIL_CHUNKS MB bhi padhta hai aur sparse temp file banakar dobara parse karta hai.
    Returns (languages, unresolved_tracks, audio_track_count)
    """
    global _flood_until
    if time.time() < _flood_until:
        return [], 0, 0
    total = int(getattr(media, "file_size", 0) or 0)
    path = os.path.join(tempfile.gettempdir(), f"lens_{uuid.uuid4().hex}.tmp")

    async def _run():
        async with aiofiles.open(path, "wb") as f:
            async for chunk in client.stream_media(message, limit=SCAN_CHUNKS):
                await f.write(chunk)
        res = _read_langs(await asyncio.to_thread(MediaInfo.parse, path))

        need_tail = (res[2] == 0 or res[1] > 0) and total > (SCAN_CHUNKS + TAIL_CHUNKS) * CHUNK
        if need_tail:
            first_tail = (total + CHUNK - 1) // CHUNK - TAIL_CHUNKS
            async with aiofiles.open(path, "r+b") as f:
                await f.seek(first_tail * CHUNK)
                async for chunk in client.stream_media(message, limit=TAIL_CHUNKS, offset=first_tail):
                    await f.write(chunk)
            os.truncate(path, total)  # sparse: disk par sirf ~14 MB lagta hai
            res2 = _read_langs(await asyncio.to_thread(MediaInfo.parse, path))
            if res2[2] > 0 and (res[2] == 0 or res2[1] < res[1] or len(res2[0]) > len(res[0])):
                res = res2
        return res

    try:
        return await asyncio.wait_for(_run(), timeout=SCAN_TIMEOUT)
    except FloodWait as e:
        _flood_until = time.time() + e.value
        logger.warning("Lens scan FloodWait %ss - filename se language nikalenge", e.value)
    except Exception as e:
        logger.warning("Lens scan failed: %s", e)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return [], 0, 0


def _hashtags(langs):
    tags = []
    for n in langs:
        tag = "#" + re.sub(r"[^0-9A-Za-z]", "", n)
        if len(tag) > 1 and tag not in tags:
            tags.append(tag)
    return ", ".join(tags)


def build_caption(message, media, langs, fallback_text):
    file_name = getattr(media, "file_name", "") or ""
    if message.caption:
        base = message.caption.html
    else:
        base = f"<b>{html.escape(file_name)}</b>" if file_name else ""

    line = ""
    if langs:
        line = f"<blockquote>🔊 {_hashtags(langs)}</blockquote>"
    elif fallback_text:
        line = f"<blockquote>🔊 {fallback_text}</blockquote>"

    if line and "🔊" in base:
        line = ""
    if len(re.sub(r"<[^>]+>", "", base + line)) > 1000:  # Telegram caption limit 1024
        base = f"<b>{html.escape(file_name)}</b>" if file_name else ""
    return f"{base}\n\n{line}".strip() if base and line else (base or line)


async def _process(client, message):
    media = message.video or message.document or message.audio
    if not media:
        return

    langs, unresolved, n_audio = await scan_audio_languages(client, message, media)

    text = f"{getattr(media, 'file_name', '') or ''} {message.caption or ''}"
    if not langs or unresolved or n_audio == 0:
        # file ke andar language tag nahi mila (und) -> file name / caption se poori karo
        for name in detect_in_text(text):
            if name not in langs:
                langs.append(name)

    fallback_text = ""
    if not langs:
        if detect_langs(text) == ["Dual Audio"]:
            fallback_text = "#DualAudio"
        elif SHOW_UNKNOWN:
            fallback_text = "#Unknown"

    caption = build_caption(message, media, langs, fallback_text)

    new = None
    for _ in range(2):
        try:
            new = await message.copy(
                message.chat.id, caption=caption, parse_mode=enums.ParseMode.HTML
            )
            break
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception:
            logger.exception("Lens: file wapis bhejne mein error")
            return

    if new is None:
        return
    _own_ids.append((new.chat.id, new.id))

    if DELETE_ORIGINAL:
        try:
            await message.delete()
        except Exception as e:
            logger.warning("Lens: original delete nahi hua (bot ko Delete messages permission do): %s", e)


async def _worker(client, chat_id):
    """Chat ki files ko upload order (message id ascending) mein ek-ek karke process karta hai."""
    try:
        await asyncio.sleep(COLLECT_DELAY)  # thodi der ruko taaki saari files queue mein aa jaayein
        while True:
            heap = _queues[chat_id]
            if not heap:
                return
            _, _, message = heapq.heappop(heap)
            try:
                await _process(client, message)
            except Exception:
                logger.exception("Lens: process error")
    finally:
        _workers.pop(chat_id, None)


@Client.on_message(
    filters.chat(LENS_CHANNELS) & (filters.document | filters.video | filters.audio) & ~filters.me,
    group=-5,
)
async def lens_handler(client, message):
    if (message.chat.id, message.id) in _own_ids:
        return
    cid = message.chat.id
    heapq.heappush(_queues[cid], (message.id, next(_counter), message))
    if cid not in _workers:
        _workers[cid] = asyncio.create_task(_worker(client, cid))


logger.info("Lens plugin loaded. Chats: %s | delete original: %s", LENS_CHANNELS, DELETE_ORIGINAL)

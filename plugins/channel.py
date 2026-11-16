import re
import logging
import asyncio
from datetime import datetime
from collections import defaultdict
from plugins.Dreamxfutures.Imdbposter import get_movie_detailsx, fetch_image, get_movie_details
from difflib import SequenceMatcher as _SM
from database.users_chats_db import db
from pyrogram import Client, filters, enums
from io import BytesIO
from info import CHANNELS, MOVIE_UPDATE_CHANNEL, LINK_PREVIEW, ABOVE_PREVIEW, BAD_WORDS, LANDSCAPE_POSTER, TMDB_POSTER, ADMINS
from Script import script
from database.ia_filterdb import save_file, get_bad_files
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
from utils import temp
from pymongo.errors import PyMongoError, DuplicateKeyError
from pyrogram.errors import MessageIdInvalid, MessageNotModified, FloodWait, UserIsBlocked, InputUserDeactivated, PeerIdInvalid
from typing import Optional, Tuple
from urllib.parse import quote_plus
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# Hamesha DB file ka hi cover/thumbnail use hoga, TMDB/IMDb poster kabhi nahi aayega
POSTER_SOURCE = "file"

def _title_similarity(a, b) -> float:
    a = re.sub(r"[^a-z0-9 ]", "", (a or "").lower()).strip()
    b = re.sub(r"[^a-z0-9 ]", "", (b or "").lower()).strip()
    if not a or not b:
        return 0.0
    return _SM(None, a, b).ratio()

IGNORE_WORDS = {
    "rarbg", "dub", "sub", "sample", "mkv", "aac", "combined",
    "hdcam", "hdtc", "camrip", "ts", "tc",
    "telesync", "dvdscr", "dvdrip", "predvd", "webrip", "web-dl", "tvrip",
    "hdtv", "web dl", "webdl", "bluray", "brrip", "bdrip", "360p", "480p",
    "720p", "1080p", "2160p", "4k", "1440p", "540p", "240p", "140p", "hevc",
    "hdrip", "hin", "hindi", "tam", "tamil", "kan", "kannada", "tel", "telugu",
    "mal", "malayalam", "eng", "english", "pun", "punjabi", "bengali",
    "mar", "marathi", "guj", "gujarati", "urd", "urdu", "kor", "korean", "jpn",
    "japanese", "nf", "netflix", "sonyliv", "sliv", "amzn",
    "primevideo", "hotstar", "zee5", "jio", "jhs", "hbo",
    "hoichoi", "sunnxt", "cinevood", "esub", "hc-esub", "x264", "x265", "10bit"
} | BAD_WORDS

CAPTION_LANGUAGES = {
    "hin": "Hindi", "hindi": "Hindi",
    "tam": "Tamil", "tamil": "Tamil",
    "kan": "Kannada", "kannada": "Kannada",
    "tel": "Telugu", "telugu": "Telugu",
    "mal": "Malayalam", "malayalam": "Malayalam",
    "eng": "English", "english": "English",
    "pun": "Punjabi", "punjabi": "Punjabi",
    "ben": "Bengali", "bengali": "Bengali",
    "mar": "Marathi", "marathi": "Marathi",
    "guj": "Gujarati", "gujarati": "Gujarati",
    "urd": "Urdu", "urdu": "Urdu",
    "kor": "Korean", "korean": "Korean",
    "jpn": "Japanese", "japanese": "Japanese",
}

OTT_PLATFORMS = {
    "nf": "Netflix", "netflix": "Netflix",
    "sonyliv": "SonyLiv", "sony": "SonyLiv", "sliv": "SonyLiv",
    "amzn": "Amazon Prime Video", "prime": "Amazon Prime Video", "primevideo": "Amazon Prime Video",
    "hotstar": "Disney+ Hotstar", "zee5": "Zee5",
    "jio": "JioHotstar", "jhs": "JioHotstar",
    "aha": "Aha", "hbo": "HBO Max", "paramount": "Paramount+",
    "apple": "Apple TV+", "hoichoi": "Hoichoi", "sunnxt": "Sun NXT", "viki": "Viki"
}

STANDARD_GENRES = {
    'Action', 'Adventure', 'Animation', 'Biography', 'Comedy', 'Crime', 'Documentary',
    'Drama', 'Family', 'Fantasy', 'Film-Noir', 'History', 'Horror', 'Music',
    'Musical', 'Mystery', 'Romance', 'Sci-Fi', 'Sport', 'Thriller', 'War', 'Western'
}

CLEAN_PATTERN = re.compile(r'@[^ \n\r\t\.,:;!?()\[\]{}<>\\/"\'=_%]+|\bwww\.[^\s\]\)]+|\([\@^]+\)|\[[\@^]+\]')
NORMALIZE_PATTERN = re.compile(r"[._]+|[()\[\]{}:;'–!,.?_]")
QUALITY_PATTERN = re.compile(
    r"\b(?:HDCam|HDTC|CamRip|TS|TC|TeleSync|DVDScr|DVDRip|PreDVD|"
    r"WEBRip|WEB-DL|TVRip|HDTV|WEB DL|WebDl|BluRay|BRRip|BDRip|"
    r"360p|480p|720p|1080p|2160p|4K|1440p|540p|240p|140p|HEVC|HDRip)\b", 
    re.IGNORECASE
)
YEAR_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])")
RESOLUTION_TERMS = {"360p", "480p", "720p", "1080p", "2160p", "4k", "1440p", "540p", "240p", "140p"}
RANGE_REGEX = re.compile(r'\bS(\d{1,2})[^\w\n\r]*E(?:p(?:isode)?)?0*(\d{1,3})\s*(?:to|-)\s*(?:E(?:p(?:isode)?)?)?0*(\d{1,3})', re.IGNORECASE)
SINGLE_REGEX = re.compile(r'\bS(\d{1,2})[^\w\n\r]*E(?:p(?:isode)?)?0*(\d{1,3})', re.IGNORECASE)
NAMED_REGEX = re.compile(r'Season\s*0*(\d{1,2})[\s\-,:]*Ep(?:isode)?\s*0*(\d{1,3})', re.IGNORECASE)
EP_ONLY_RANGE = re.compile(r'\b(?:EP|Episode)0*(\d{1,3})\s*-\s*0*(\d{1,3})\b', re.IGNORECASE)

MEDIA_FILTER = filters.document | filters.video | filters.audio
locks = defaultdict(asyncio.Lock)
pending_updates = {}
error_tmdb = False

def clean_mentions_links(text: str) -> str:
    return CLEAN_PATTERN.sub("", text or "").strip()

def normalize(s: str) -> str:
    s = NORMALIZE_PATTERN.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()

def remove_ignored_words(text: str) -> str:
    IGNORE_WORDS_LOWER = {w.lower() for w in IGNORE_WORDS}
    return " ".join(word for word in text.split() if word.lower() not in IGNORE_WORDS_LOWER)

def get_qualities(text: str) -> str:
    qualities = QUALITY_PATTERN.findall(text)
    return ", ".join(qualities) if qualities else "N/A"

def extract_ott_platform(text: str) -> str:
    text = text.lower()
    platforms = {plat for key, plat in OTT_PLATFORMS.items() if key in text}
    return " | ".join(platforms) if platforms else "N/A"

def extract_season_episode(filename: str) -> Tuple[Optional[int], Optional[str]]:
    if m := EP_ONLY_RANGE.search(filename):
        return 1, f"{int(m.group(1))}-{int(m.group(2))}"
    for pattern in (RANGE_REGEX, SINGLE_REGEX, NAMED_REGEX):
        if m := pattern.search(filename):
            season = int(m.group(1))
            if pattern == RANGE_REGEX:
                ep = f"{m.group(2)}-{m.group(3)}"
            else:
                ep = m.group(2)
            return season, ep
    return None, None


async def _download_any_thumb(bot, obj) -> Optional[bytes]:
    """Kisi bhi photo/thumb/cover object se bytes download karta hai."""
    if not obj:
        return None
    try:
        # Agar list hai (jaise thumbs ya sizes), toh sabse bada (last) size lo
        target = obj[-1] if isinstance(obj, (list, tuple)) else obj
        file_id = getattr(target, "file_id", None)
        if not file_id and hasattr(target, "thumbs") and target.thumbs:
            file_id = target.thumbs[-1].file_id
        if not file_id:
            return None
        data = await bot.download_media(file_id, in_memory=True)
        if not data:
            return None
        raw = data.getvalue() if hasattr(data, "getvalue") else bytes(data)
        return raw if raw else None
    except Exception as e:
        logger.warning(f"Thumb download warning: {e}")
        return None


async def _get_file_cover(bot, message=None, media=None) -> Optional[bytes]:
    """
    Database file se Cover ya Thumbnail nikalne ka 100% paka tarika:
    1. Video Cover (Telegram ke naye video covers: message.video_cover / media.video_cover / media.cover)
    2. Media Thumbs (message.video.thumbs / message.document.thumbs / media.thumbs)
    3. Message Photo / Thumbs
    4. Agar message fresh fetch karna pade toh bot.get_messages se laakar check karega.
    """
    try:
        # Step 1: Check message-level & media-level video_cover / cover (New Telegram Video Cover feature)
        for container in (message, media):
            if not container:
                continue
            for attr in ("video_cover", "cover", "photo"):
                cov = getattr(container, attr, None)
                if cov:
                    raw = await _download_any_thumb(bot, cov)
                    if raw:
                        return raw

        # Step 2: Check standard thumbs on media (video.thumbs / document.thumbs / audio.thumbs)
        for container in (media, getattr(message, "video", None), getattr(message, "document", None), getattr(message, "audio", None)):
            if not container:
                continue
            thumbs = getattr(container, "thumbs", None)
            if thumbs:
                raw = await _download_any_thumb(bot, thumbs)
                if raw:
                    return raw
            thumb = getattr(container, "thumb", None)
            if thumb:
                raw = await _download_any_thumb(bot, thumb)
                if raw:
                    return raw

        # Step 3: Agar message hai par Pyrogram ne cache ki wajah se cover nahi diya, re-fetch message
        if message and getattr(message, "chat", None) and getattr(message, "id", None):
            try:
                fresh_msg = await bot.get_messages(message.chat.id, message.id)
                if fresh_msg:
                    for attr in ("video_cover", "cover", "photo"):
                        cov = getattr(fresh_msg, attr, None)
                        if cov:
                            raw = await _download_any_thumb(bot, cov)
                            if raw:
                                return raw
                    for ft in ("video", "document", "audio"):
                        m_obj = getattr(fresh_msg, ft, None)
                        if m_obj:
                            for sub_attr in ("video_cover", "cover", "thumbs", "thumb"):
                                sub_val = getattr(m_obj, sub_attr, None)
                                if sub_val:
                                    raw = await _download_any_thumb(bot, sub_val)
                                    if raw:
                                        return raw
            except Exception as e:
                logger.warning(f"Fresh message fetch for cover failed: {e}")

    except Exception as e:
        logger.warning(f"File cover extraction failed: {e}")
    return None


@Client.on_message(filters.command("announce") & filters.user(ADMINS))
async def announce_movie_update(bot, message):
    try:
        raw = message.text.split(" ", 1)[1].strip()
    except IndexError:
        await message.reply_text(
            "<b>Movie ka naam likho command ke saath, jaise:</b>\n"
            "<code>/announce Mirzapur</code>\n\n"
            "<b>Ek se zyada movies ek saath karni hon, to har naam nayi line pe likho:</b>\n"
            "<code>/announce\nMirzapur\nHercai\nSingham Again</code>"
        )
        return
    queries = [q.strip() for q in raw.split("\n") if q.strip()]
    sts = await message.reply_text(f"<b>🔎 {len(queries)} movie(s) process ho rahi hain...</b>")
    posted, not_found, failed = [], [], []
    for query in queries:
        files, total = await get_bad_files(query)
        if not files:
            not_found.append(query)
            continue
        f = files[0]
        file_name = getattr(f, "file_name", "")
        caption = getattr(f, "caption", None) or ""
        try:
            media_info = extract_media_info(file_name, caption)
            base_name = media_info["base_name"]
            existing = await db.movie_updates.find_one({"_id": base_name})
            if existing:
                refreshed_files = []
                for old_file in existing.get("files", []):
                    fn = old_file.get("filename", "")
                    info = extract_media_info(fn, "")
                    refreshed_files.append({
                        "filename": fn,
                        "processed": info["processed"],
                        "quality": info["quality"],
                        "language": info["language"],
                        "ott_platform": info["ott_platform"],
                        "timestamp": old_file.get("timestamp", datetime.now()),
                        "tag": info["tag"],
                        "season": info["season"],
                        "episode": info["episode"],
                    })
                await db.movie_updates.update_one(
                    {"_id": base_name},
                    {"$set": {"files": refreshed_files}}
                )
                await send_movie_update(bot, base_name)
            else:
                await process_and_send_update(bot, file_name, caption)
            posted.append(query)
        except Exception as e:
            logger.exception("Error in /announce for %s", query)
            failed.append(query)

    result_lines = []
    if posted:
        result_lines.append(f"<b>✅ Posted ({len(posted)}):</b>\n" + "\n".join(f"• {q}" for q in posted))
    if not_found:
        result_lines.append(f"<b>❌ Database mein nahi mili ({len(not_found)}):</b>\n" + "\n".join(f"• {q}" for q in not_found))
    if failed:
        result_lines.append(f"<b>❗ Error aaya ({len(failed)}):</b>\n" + "\n".join(f"• {q}" for q in failed))
    await sts.edit("\n\n".join(result_lines))


async def notify_pending_requesters(bot, file_name):
    pending = await db.get_all_movie_requests()
    if not pending:
        return
    clean_name = normalize(clean_mentions_links(file_name)).lower()
    for req in pending:
        query_text = (req.get('query') or '').strip().lower()
        if not query_text:
            continue
        score = fuzz.partial_ratio(query_text, clean_name)
        if score < 75:
            continue
        try:
            claimed = await db.delete_movie_request(req['_id'])
        except Exception:
            logger.exception("Could not claim movie request %s", req.get('_id'))
            continue
        if not claimed:
            continue
        try:
            encoded = quote_plus(req['query'])[:50]
            await bot.send_message(
                chat_id=req['user_id'],
                text=script.MOVIE_REQUEST_FOUND.format(req.get('user_mention', 'ᴜsᴇʀ'), req['query']),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Dᴇᴋʜᴏ Aʙʜɪ", callback_data=f"notifyme_search#{encoded}")]
                ]),
                parse_mode=enums.ParseMode.HTML
            )
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
            pass
        except Exception:
            logger.exception("Could not notify user %s for movie request", req.get('user_id'))


def schedule_update(bot, base_name, delay=5):
    if handle := pending_updates.get(base_name):
        if not handle.cancelled():
            handle.cancel()
    
    loop = asyncio.get_event_loop()
    pending_updates[base_name] = loop.call_later(
        delay,
        lambda: asyncio.create_task(update_movie_message(bot, base_name))
    )


def _pick_year(filename: str, unified: str):
    low = filename.lower()
    matches = list(YEAR_PATTERN.finditer(low))
    if matches:
        q = QUALITY_PATTERN.search(filename)
        before = [m for m in matches if q is None or m.start() < q.start()]
        m = (before or matches)[-1] if before else matches[0]
        return m.group(0), m.start()
    cm = YEAR_PATTERN.search(unified)
    if cm:
        return cm.group(0), low.find(cm.group(0))
    return None, -1


_LANG_FULL = {k: v for k, v in CAPTION_LANGUAGES.items() if len(k) > 3}
_LANG_CODES = {k: v for k, v in CAPTION_LANGUAGES.items() if len(k) == 3}
_LANG_NAMES = "|".join(sorted(_LANG_FULL, key=len, reverse=True))
_LANG_FULL_RE = re.compile(r"(?<![a-z])(" + _LANG_NAMES + r")(?![a-z])|(?<![a-z])(" + _LANG_NAMES + r")(?=dub|sub)")
_LANG_SUBTITLE_RE = re.compile(r"(?<![a-z])(?:" + "|".join(sorted(CAPTION_LANGUAGES, key=len, reverse=True)) + r")[\s._+-]*(?:subs?|subtitles?)(?![a-z])")
_TECH_START_RE = re.compile(YEAR_PATTERN.pattern + "|" + QUALITY_PATTERN.pattern, re.IGNORECASE)


def _find_languages(caption_text: str, filename_text: str) -> str:
    found = set()
    for text in (caption_text or "", filename_text or ""):
        t = _LANG_SUBTITLE_RE.sub(" ", text.lower())
        for m in _LANG_FULL_RE.finditer(t):
            found.add(_LANG_FULL[m.group(1) or m.group(2)])
        start = _TECH_START_RE.search(t)
        if start:
            for tok in re.findall(r"[a-z]+", t[start.start():]):
                if tok in _LANG_CODES:
                    found.add(_LANG_CODES[tok])
    return ", ".join(sorted(found)) if found else "N/A"


def _list_items(value):
    if isinstance(value, (list, tuple, set)):
        items = [str(x).strip() for x in value]
    else:
        items = [x.strip() for x in str(value or "").split(",")]
    return [i for i in items if i and i.upper() != "N/A"]


def _details_ok(details, base_name, media_year, media_lang=None) -> bool:
    """
    Strict Verification:
    1. Title कम से कम 88% match hona chahiye.
    2. Agar file mein Year (jaise 2026) hai, toh TMDB/IMDb result mein bhi Year hona zaroori hai aur exact match (max 1 year diff) hona chahiye.
    3. Agar file mein regional Indian language (Marathi, Punjabi, Bengali, Gujarati, Kannada, Malayalam, Tamil, Telugu) hai
       aur TMDB/IMDb result Japanese/Korean/Chinese/Animation hai, toh turant reject kar dega.
    """
    if not details or details.get("error"):
        return False

    title = details.get("title") or details.get("localized_title")
    if not title:
        return False

    wanted = re.sub(r"\s*\(?\b(?:19|20)\d{2}\b\)?\s*$", "", base_name or "").strip()
    sim_title = _title_similarity(wanted, title)
    sim_local = _title_similarity(wanted, details.get("localized_title") or "")
    if max(sim_title, sim_local) < 0.88:
        return False

    # Strict Year Check
    if media_year:
        try:
            fy = int(media_year)
            dy = int(details.get("year") or 0)
            if not dy or abs(dy - fy) > 1:
                return False
        except (TypeError, ValueError):
            return False

    # Language & Country sanity check (prevent random Japanese Anime matching Indian movies)
    if media_lang and media_lang != "N/A":
        file_langs = {l.strip().lower() for l in media_lang.split(",") if l.strip()}
        indian_langs = {"hindi", "marathi", "tamil", "telugu", "kannada", "malayalam", "punjabi", "bengali", "gujarati", "urdu"}
        if file_langs & indian_langs:
            db_langs = {l.strip().lower() for l in _list_items(details.get("languages"))}
            db_countries = {c.strip().lower() for c in _list_items(details.get("countries"))}
            db_genres = {g.strip().lower() for g in _list_items(details.get("genres"))}
            if ("japan" in db_countries or "japanese" in db_langs or "animation" in db_genres) and "india" not in db_countries:
                return False
            if db_langs and not (file_langs & db_langs) and "india" not in db_countries:
                return False

    return True


def extract_media_info(filename: str, caption: str):
    filename = normalize(clean_mentions_links(filename).title())
    caption_clean = clean_mentions_links(caption).lower() if caption else ""
    unified = f"{caption_clean} {filename.lower()}".strip()

    season = episode = year = None
    tag = "#MOVIE"
    processed_raw = base_raw = filename
    quality = get_qualities(caption_clean)
    if quality == "N/A":
        quality = get_qualities(filename.lower())
    ott_platform = extract_ott_platform(f"{filename} {caption_clean}")

    language = _find_languages(caption_clean, filename.lower())

    season, episode = extract_season_episode(filename)
    if season is not None:
        tag = "#SERIES"
        if m := (RANGE_REGEX.search(filename) or SINGLE_REGEX.search(filename) or NAMED_REGEX.search(filename) or EP_ONLY_RANGE.search(filename)):
            match_str = m.group(0)
            start_idx = filename.lower().find(match_str.lower())
            end_idx = start_idx + len(match_str)
            processed_raw = filename[:end_idx]
            base_raw = filename[:start_idx]
            if year_match := YEAR_PATTERN.search(filename.lower()[end_idx:]):
                y = year_match.group(0)
                yi = filename.lower().find(y, end_idx)
                if yi != -1:
                    processed_raw = filename[:yi+4]
                    base_raw += f" {y}"
    else:
        year, year_idx = _pick_year(filename, unified)
        if year:
            if year_idx != -1:
                processed_raw = filename[:year_idx + 4]
                base_raw = processed_raw
        else:
            if qual_match := QUALITY_PATTERN.search(unified):
                qual_str = qual_match.group(0)
                qual_idx = filename.lower().find(qual_str.lower())
                if qual_idx != -1:
                    processed_raw = filename[:qual_idx]
                    base_raw = processed_raw

    base_name = normalize(remove_ignored_words(normalize(base_raw)))
    if year and year not in base_name:
        base_name += f" {year}"

    if base_name.endswith(")"):
        base_name = re.sub(r"\s+\(\d{4}\)$", "", base_name)
        if year:
            base_name += f" {year}"

    def _strip_season_episode_tokens(name: str) -> str:
        if not name:
            return name
        year_match = re.search(r'\(?\b(19|20)\d{2}\b\)?\s*$', name)
        year_part = ""
        if year_match:
            year_part = year_match.group(0)
            name = name[:year_match.start()].strip()

        patterns = [
            r'\bS\d{1,2}E\d{1,2}\b',
            r'\bS\d{1,2}\b',
            r'\bE\d{1,2}\b',
            r'\b\d{1,2}x\d{1,2}\b',
            r'\bSeason\s*\d{1,2}\b',
            r'\bEp(?:isode)?\.?\s*\d{1,3}\b',
            r'\bEpisode\s*\d{1,3}\b',
            r'\bPart\s*\d{1,2}\b'
        ]
        for p in patterns:
            name = re.sub(p, ' ', name, flags=re.IGNORECASE)

        name = re.sub(r'[_\.\-]+', ' ', name)
        name = re.sub(r'\s+', ' ', name).strip()

        if year_part:
            y = re.search(r'(19|20)\d{2}', year_part)
            if y:
                name = f"{name} {y.group(0)}"
        return name.strip()

    base_name = _strip_season_episode_tokens(base_name)
    if not base_name:
        base_name = normalize(remove_ignored_words(normalize(processed_raw))) or filename

    return {
        "processed": normalize(processed_raw),
        "base_name": base_name,
        "tag": tag,
        "season": season,
        "episode": episode,
        "year": year,
        "quality": quality,
        "ott_platform": ott_platform,
        "language": language
    }


@Client.on_message(filters.chat(CHANNELS) & MEDIA_FILTER)
async def media_handler(bot, message):
    media = next(
        (getattr(message, ft) for ft in ("document", "video", "audio")
         if getattr(message, ft, None)),
        None
    )
    if not media:
        return

    media.file_type = next(ft for ft in ("document", "video", "audio") if getattr(message, ft, None))
    media.caption = message.caption or ""
    success, info = await save_file(media)
    if not success:
        return

    try:
        await notify_pending_requesters(bot, media.file_name)
    except Exception:
        logger.exception("Error in notify_pending_requesters")

    try:
        if await db.movie_update_status(bot.me.id):
            # Hamesha message aur media dono check karke cover/thumbnail nikalein
            cover = await _get_file_cover(bot, message, media)
            await process_and_send_update(bot, media.file_name, media.caption, cover)
    except Exception:
        logger.exception("Error processing media")


async def _send_cover(bot, movie_doc, text, buttons):
    """
    Update channel mein DB file ke cover/thumbnail ke saath post bhejta hai.
    LINK_PREVIEW kuch bhi ho, agar file mein cover/thumbnail hai toh photo post hi jayega!
    """
    cover = movie_doc.get("poster_bytes")
    if not cover:
        return None
    try:
        bio = BytesIO(bytes(cover))
        bio.name = "cover.jpg"
        return await bot.send_photo(
            chat_id=MOVIE_UPDATE_CHANNEL,
            photo=bio,
            caption=text,
            reply_markup=buttons,
            parse_mode=enums.ParseMode.HTML
        )
    except FloodWait:
        raise
    except Exception as e:
        logger.warning(f"Cover post failed: {e}")
        return None


async def process_and_send_update(bot, filename, caption, cover=None):
    try:
        media_info = extract_media_info(filename, caption)
        base_name = media_info["base_name"]
        processed = media_info["processed"]

        lock = locks[base_name]
        async with lock:
            await _process_with_lock(bot, filename, caption, media_info, base_name, processed, cover)
    except PyMongoError as e:
        logger.error(f"Database error in process_and_send_update: {e}")
        raise
    except Exception as e:
        logger.exception(f"Processing failed in process_and_send_update: {e}")
        raise


async def _process_with_lock(bot, filename, caption, media_info, base_name, processed, cover=None):
    if not hasattr(db, 'movie_updates'):
        db.movie_updates = db.db.movie_updates

    movie_doc = await db.movie_updates.find_one({"_id": base_name})
    error_tmdb = False
    file_data = {
        "filename": filename,
        "processed": processed,
        "quality": media_info["quality"],
        "language": media_info["language"],
        "ott_platform": media_info["ott_platform"],
        "timestamp": datetime.now(),
        "tag": media_info["tag"],
        "season": media_info["season"],
        "episode": media_info["episode"]
    }

    if not movie_doc:
        details = {}
        if TMDB_POSTER:
            d = await get_movie_detailsx(base_name)
            if _details_ok(d, base_name, media_info["year"], media_info["language"]):
                details = d
            else:
                error_tmdb = True
                logger.info(f"TMDB match rejected for '{base_name}', trying IMDb")
        if not details:
            d = await get_movie_details(base_name) or {}
            if _details_ok(d, base_name, media_info["year"], media_info["language"]):
                details = d
            else:
                logger.info(f"No accurate movie details for '{base_name}' - hiding wrong genre/cast")

        raw_genres = details.get("genres", "N/A")
        if isinstance(raw_genres, str):
            genre_list = [g.strip() for g in raw_genres.split(",")]
            genres = ", ".join(g for g in genre_list if g in STANDARD_GENRES) or "N/A"
        else:
            genres = ", ".join(g for g in raw_genres if g in STANDARD_GENRES) or "N/A"

        movie_doc = {
            "_id": base_name,
            "files": [file_data],
            "poster_url": None,  # TMDB/IMDb poster disabled so wrong poster never shows
            "genres": genres,
            "cast": ", ".join(_list_items(details.get("cast"))[:5]) or "N/A",
            "story": details.get("plot") or "N/A",
            "rating": details.get("rating", "N/A"),
            "imdb_url": "",
            "year": details.get("year") or media_info["year"],
            "tag": media_info["tag"],
            "ott_platform": media_info["ott_platform"],
            "message_id": None,
            "is_photo": False,
            "error_tmdb": error_tmdb,
            "is_backdrop": False,
            "poster_bytes": cover if cover else None
        }
        try:
            await db.movie_updates.insert_one(movie_doc)
            await send_movie_update(bot, base_name)
            movie_doc = await db.movie_updates.find_one({"_id": base_name})
        except DuplicateKeyError:
            movie_doc = await db.movie_updates.find_one({"_id": base_name})
            if movie_doc:
                if any(f["filename"] == filename for f in movie_doc["files"]):
                    return
                update_fields = {"$push": {"files": file_data}}
                if cover and not movie_doc.get("poster_bytes"):
                    update_fields["$set"] = {"poster_bytes": cover}
                await db.movie_updates.update_one({"_id": base_name}, update_fields)
                await update_movie_message(bot, base_name)
    else:
        if any(f["filename"] == filename for f in movie_doc["files"]):
            if cover and not movie_doc.get("poster_bytes"):
                await db.movie_updates.update_one(
                    {"_id": base_name},
                    {"$set": {"poster_bytes": cover}}
                )
                await update_movie_message(bot, base_name)
            return

        update_fields = {"$push": {"files": file_data}}
        if cover and not movie_doc.get("poster_bytes"):
            update_fields["$set"] = {"poster_bytes": cover}

        await db.movie_updates.update_one({"_id": base_name}, update_fields)
        await update_movie_message(bot, base_name)


async def send_movie_update(bot, base_name):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            movie_doc = await db.movie_updates.find_one({"_id": base_name})
            if not movie_doc:
                return None

            text = generate_movie_message(movie_doc, base_name)
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    '❗ ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ɢᴇᴛ ғɪʟᴇ ❗',
                    url=f"https://t.me/{temp.U_NAME}?start=getfile-{base_name.replace(' ', '-')}"
                )
            ]])

            # Sirf DB file ka cover/thumbnail send hoga
            cover_msg = await _send_cover(bot, movie_doc, text, buttons)
            if cover_msg is not None:
                msg = cover_msg
                is_photo = True
            else:
                # Agar DB file mein koi cover/thumbnail nahi hai, toh clean text post jayega (bina kisi galat poster ke)
                send_params = {
                    "chat_id": MOVIE_UPDATE_CHANNEL,
                    "text": text,
                    "reply_markup": buttons,
                    "parse_mode": enums.ParseMode.HTML,
                    "link_preview_options": LinkPreviewOptions(is_disabled=True)
                }
                msg = await bot.send_message(**send_params)
                is_photo = False

            await db.movie_updates.update_one(
                {"_id": base_name},
                {"$set": {"message_id": msg.id, "is_photo": is_photo}}
            )
            return msg
        except FloodWait as e:
            wait_time = e.value + 2
            await asyncio.sleep(wait_time)
        except Exception as e:
            logger.error(f"Failed to send movie update: {e}")
            break
    return None


async def update_movie_message(bot, base_name):
    try:
        movie_doc = await db.movie_updates.find_one({"_id": base_name})
        if not movie_doc:
            return

        text = generate_movie_message(movie_doc, base_name)
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                '❗ ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ɢᴇᴛ ғɪʟᴇ ❗',
                url=f"https://t.me/{temp.U_NAME}?start=getfile-{base_name.replace(' ', '-')}"
            )
        ]])

        message_id = movie_doc.get("message_id")
        is_photo = movie_doc.get("is_photo", False)
        has_cover = bool(movie_doc.get("poster_bytes"))

        if not message_id:
            await send_movie_update(bot, base_name)
            return

        # Agar pehle text message gaya tha (kyunki 1st file mein cover nahi tha)
        # aur ab 2nd file mein cover aa gaya hai, toh purana text message delete karke Photo ke saath naya post bhejo!
        if not is_photo and has_cover:
            try:
                await bot.delete_messages(chat_id=MOVIE_UPDATE_CHANNEL, message_ids=message_id)
            except Exception:
                pass
            await db.movie_updates.update_one(
                {"_id": base_name},
                {"$set": {"message_id": None, "is_photo": False}}
            )
            await send_movie_update(bot, base_name)
            return

        try:
            if is_photo:
                await bot.edit_message_caption(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    message_id=message_id,
                    caption=text,
                    reply_markup=buttons,
                    parse_mode=enums.ParseMode.HTML
                )
            else:
                await bot.edit_message_text(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    message_id=message_id,
                    text=text,
                    reply_markup=buttons,
                    parse_mode=enums.ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            return
        except (MessageIdInvalid, MessageNotModified) as e:
            logger.warning(f"Message update skipped due to error: {e}")
            pass
        except Exception:
            try:
                await bot.delete_messages(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    message_ids=message_id
                )
                await db.movie_updates.update_one(
                    {"_id": base_name},
                    {"$set": {"message_id": None, "is_photo": False}}
                )
            except Exception as e:
                logger.error(f"Error during message deletion/update in recovery: {e}")
                pass
            await send_movie_update(bot, base_name)
    except Exception as e:
        logger.error(f"Failed to update movie message for {base_name}: {e}")


def generate_movie_message(movie_doc, base_name):
    all_qualities = set()
    all_languages = set()
    all_ott_platforms = set()
    all_tags = set()
    episodes_by_season = defaultdict(set)

    for file in movie_doc["files"]:
        if file["quality"] != "N/A":
            all_qualities.update(q.strip() for q in file["quality"].split(",") if q.strip())
        if file["language"] != "N/A":
            all_languages.update(lang.strip() for lang in file["language"].split(",") if lang.strip())
        if file["ott_platform"] != "N/A":
            platforms = [p.strip() for p in file["ott_platform"].split("|") if p.strip()]
            all_ott_platforms.update(platforms)
        if file["tag"]:
            all_tags.add(file["tag"])
        if file.get("season") and file.get("episode"):
            season = file["season"]
            episode = file["episode"]
            episodes_by_season[season].add(episode)

    primary_tag = "#SERIES" if "#SERIES" in all_tags else "#MOVIE"
    season_episode_block = ""
    if episodes_by_season:
        season_lines = []
        for season, episodes in sorted(episodes_by_season.items(), key=lambda x: int(x[0])):
            singles = []
            ranges = []

            for ep in episodes:
                if "-" in ep:
                    ranges.append(ep)
                else:
                    try:
                        singles.append(int(ep))
                    except ValueError:
                        ranges.append(ep)

            singles.sort()
            collapsed = []
            start = end = None
            for num in singles:
                if start is None:
                    start = end = num
                elif num == end + 1:
                    end = num
                else:
                    collapsed.append(str(start) if start == end else f"{start}-{end}")
                    start = end = num
            if start is not None:
                collapsed.append(str(start) if start == end else f"{start}-{end}")

            all_ep_parts = collapsed + sorted(ranges, key=lambda s: int(s.split("-")[0]))
            episode_str = ", ".join(all_ep_parts)
            season_lines.append(
                f"☀️ Sᴇᴀsᴏɴ : <b>S{int(season):02d}</b>\n📄 Eᴘɪsᴏᴅᴇ : <b>{episode_str}</b>"
            )
        if season_lines:
            season_episode_block = "\n".join(season_lines) + "\n"

    import html as _html
    genres_list = _list_items(movie_doc.get("genres"))
    cast_list = _list_items(movie_doc.get("cast"))[:5]
    story = str(movie_doc.get("story") or "").strip()
    if story.upper() == "N/A":
        story = ""
    if len(story) > 280:
        story = story[:277].rstrip() + "..."

    resolutions, formats = set(), set()
    for q in all_qualities:
        (resolutions if q.lower() in RESOLUTION_TERMS else formats).add(q)
    quality_str = " + ".join(sorted(resolutions, key=lambda x: int(re.sub(r'\D', '', x) or 0))) if resolutions else ""
    format_str = ", ".join(sorted(formats)) if formats else ""
    language_str = ", ".join(sorted(all_languages)) if all_languages else ""

    try:
        r = float(movie_doc.get("rating"))
    except (TypeError, ValueError):
        r = 0.0
    rating_text = str(movie_doc.get("rating")) if r > 0 else ""

    year_val = str(movie_doc.get("year") or "")
    filename_display = base_name
    if year_val and filename_display.strip().endswith(year_val):
        filename_display = filename_display.strip()[:-len(year_val)].strip()

    def esc(x):
        return _html.escape(str(x), quote=False)

    tech_lines = []
    if language_str:
        tech_lines.append(f"🔊 Aᴜᴅɪᴏ : <b>{esc(language_str)}</b>")
    if quality_str:
        tech_lines.append(f"🌿 Qᴜᴀʟɪᴛʏ : <b>{esc(quality_str)}</b>")
    if format_str:
        tech_lines.append(f"📌 Fᴏʀᴍᴀᴛ : <b>{esc(format_str)}</b>")
    tech_block = season_episode_block + "\n".join(tech_lines)
    tech_block = tech_block.strip("\n")

    def build_details(with_story):
        lines = []
        if rating_text:
            lines.append(f"⭐ Rᴀᴛɪɴɢ : <b>{esc(rating_text)}/10</b>")
        if genres_list:
            lines.append(f"🎭 Gᴇɴʀᴇs : <b>{esc(', '.join(genres_list))}</b>")
        if cast_list:
            lines.append(f"👥 Cᴀsᴛ : <b>{esc(', '.join(cast_list))}</b>")
        if with_story and story:
            lines.append(f"📜 Sᴛᴏʀʏ : {esc(story)}")
        return f"<blockquote expandable>{chr(10).join(lines)}</blockquote>" if lines else ""

    divider = "\n\n――――――――――――\n\n"

    def build(with_story):
        parts = [f"<blockquote>✅ <b>{esc(filename_display)} {esc(year_val)} {primary_tag}</b></blockquote>"]
        if tech_block:
            parts.append(tech_block)
        details_block = build_details(with_story)
        if details_block:
            parts.append(details_block)
        return divider.join(parts)

    text = build(True)
    visible = re.sub(r"<[^>]+>", "", text)
    if len(visible) > 950:
        text = build(False)
    return text

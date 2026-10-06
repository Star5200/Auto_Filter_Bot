import os
import random
import asyncio
import logging
from collections import OrderedDict

import pytz
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from database.users_chats_db import db, FILE_HISTORY_DAYS
from info import PICS, DELETE_TIME
from utils import temp, get_size, clean_filename

logger = logging.getLogger(__name__)

TZ = pytz.timezone("Asia/Kolkata")
MAX_FILES_PER_DATE = 40  # ek date mein max itne file buttons
LINE = "────────────────────"
# History message kitni der baad auto-delete ho (seconds). Default: DELETE_TIME jitna (movies jaisa)
HISTORY_DELETE_TIME = int(os.environ.get("HISTORY_DELETE_TIME", DELETE_TIME))


async def _delete_later(delay, *messages):
    """delay seconds ke baad diye gaye messages delete kar deta hai (error aaye to ignore)."""
    await asyncio.sleep(delay)
    for m in messages:
        try:
            await m.delete()
        except Exception:
            pass


def _to_local(ts):
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(TZ)


def group_by_date(docs):
    """Docs (newest first) -> OrderedDict {'YYYYMMDD': [docs]} (same file ek din mein ek hi baar)."""
    grouped = OrderedDict()
    seen = set()
    for d in docs:
        local = _to_local(d["ts"])
        key = local.strftime("%Y%m%d")
        mark = (key, d.get("file_id"))
        if mark in seen:
            continue
        seen.add(mark)
        grouped.setdefault(key, []).append(d)
    return grouped


def pretty_date(key):
    return f"{key[6:8]}-{key[4:6]}-{key[0:4]}"


def main_text(mention, total):
    return (
        f"├\n{LINE}\n"
        f"├ <b>Here is file history</b>\n"
        f"├\n{LINE}\n"
        f"├ <b>User:</b> {mention}\n"
        f"├ <b>History Period:</b> {FILE_HISTORY_DAYS} days\n"
        f"├ <b>Total Received Files:</b> {total} files\n"
        f"├\n{LINE}\n"
        f"├ <b>Select a date to view files</b>\n"
        f"├\n{LINE}"
    )


def date_text(key, count, shown):
    extra = f"\n├ <i>Showing latest {shown} files</i>" if count > shown else ""
    return (
        f"├\n{LINE}\n"
        f"├ <b>Files received on {pretty_date(key)}</b>\n"
        f"├ <b>Total:</b> {count} files{extra}\n"
        f"├\n{LINE}\n"
        f"├ <b>Tap a file to get it again</b>\n"
        f"├\n{LINE}"
    )


def main_buttons(grouped):
    rows = [
        [InlineKeyboardButton(f"Date: {pretty_date(k)} | Files: {len(v)}", callback_data=f"hist_d_{k}")]
        for k, v in grouped.items()
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data="start")])
    return InlineKeyboardMarkup(rows)


def file_buttons(key, files):
    rows = []
    for f in files[:MAX_FILES_PER_DATE]:
        name = clean_filename(f.get("file_name")) or f.get("file_name") or "File"
        label = f"{get_size(f.get('file_size', 0))} | {name}"
        if len(label) > 60:
            label = label[:57] + "..."
        link = f"https://t.me/{temp.U_NAME}?start=file_{f.get('grp_id', 0)}_{f['file_id']}"
        rows.append([InlineKeyboardButton(label, url=link)])
    rows.append([InlineKeyboardButton("← Back", callback_data="hist_main")])
    return InlineKeyboardMarkup(rows)


@Client.on_message(filters.command("history") & filters.incoming)
async def history_cmd(client, message):
    if not message.from_user:
        return
    if message.chat.type != enums.ChatType.PRIVATE:
        notice = await message.reply_text(
            "<b>Apni file history dekhne ke liye bot ke PM mein /history use karein.</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📂 Open History", url=f"https://t.me/{temp.U_NAME}")
            ]]),
            parse_mode=enums.ParseMode.HTML,
        )
        asyncio.create_task(_delete_later(60, notice))
        return
    docs = await db.get_file_history(message.from_user.id)
    grouped = group_by_date(docs)
    total = sum(len(v) for v in grouped.values())
    text = main_text(message.from_user.mention, total)
    markup = main_buttons(grouped)
    try:
        sent = await message.reply_photo(
            photo=random.choice(PICS),
            caption=text,
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        sent = await message.reply_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
    asyncio.create_task(_delete_later(HISTORY_DELETE_TIME, sent, message))


@Client.on_callback_query(filters.regex(r"^(hist_main|hist_d_\d{8})$"))
async def history_cb(client: Client, query: CallbackQuery):
    user = query.from_user
    docs = await db.get_file_history(user.id)
    grouped = group_by_date(docs)

    if query.data == "hist_main":
        total = sum(len(v) for v in grouped.values())
        text, markup = main_text(user.mention, total), main_buttons(grouped)
    else:
        key = query.data.split("_")[2]
        files = grouped.get(key)
        if not files:
            await query.answer("Is date ki files ab history mein nahi hain.", show_alert=True)
            total = sum(len(v) for v in grouped.values())
            text, markup = main_text(user.mention, total), main_buttons(grouped)
        else:
            shown = min(len(files), MAX_FILES_PER_DATE)
            text, markup = date_text(key, len(files), shown), file_buttons(key, files)

    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        else:
            await query.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        logger.debug("history edit: %s", e)
    try:
        await query.answer()
    except Exception:
        pass

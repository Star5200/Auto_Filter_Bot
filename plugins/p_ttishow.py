import os
import sys
import logging
import asyncio
import psutil
import bot_health
from time import time
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors.exceptions.bad_request_400 import MessageTooLong, PeerIdInvalid
from pyrogram.errors import ChatAdminRequired, MessageNotModified
from info import ADMINS, MULTIPLE_DB, LOG_CHANNEL, OWNER_LNK, MELCOW_PHOTO
from database.users_chats_db import db
from database.ia_filterdb import Media, Media2, db as db_stats, db2 as db2_stats, client, client2
from utils import get_size, temp, get_settings, get_readable_time
from Script import script
from bot import botStartTime

logger = logging.getLogger(__name__)

"""-----------------------------------------https://t.me/dreamxbotz--------------------------------------"""

@Client.on_message(filters.new_chat_members & filters.group)
async def save_group(bot, message):
    dreamx_check = [u.id for u in message.new_chat_members]
    if temp.ME in dreamx_check:
        if not await db.get_chat(message.chat.id):
            total=await bot.get_chat_members_count(message.chat.id)
            dreamx_botz = message.from_user.mention if message.from_user else "Anonymous" 
            await bot.send_message(LOG_CHANNEL, script.LOG_TEXT_G.format(message.chat.title, message.chat.id, total, dreamx_botz))       
            await db.add_chat(message.chat.id, message.chat.title)
        if message.chat.id in temp.BANNED_CHATS:

            buttons = [[InlineKeyboardButton('📌 ᴄᴏɴᴛᴀᴄᴛ ꜱᴜᴘᴘᴏʀᴛ 📌', url=OWNER_LNK)]]
            reply_markup=InlineKeyboardMarkup(buttons)
            k = await message.reply(text=script.CHAT_RESTRICTED_TXT, reply_markup=reply_markup)
            try:
                await k.pin()
            except Exception:
                pass
            await bot.leave_chat(message.chat.id)
            return
        buttons = [[
                    InlineKeyboardButton("👩‍🌾 Bot Owner 👩‍🌾", url=OWNER_LNK)
                  ]]
        reply_markup=InlineKeyboardMarkup(buttons)
        await message.reply_text(
            text=script.BOT_ADD_TXT.format(message.chat.title),
            reply_markup=reply_markup)
        try:
            await db.connect_group(message.chat.id, message.from_user.id)
        except Exception as e:
            logger.error(f"DB error connecting group: {e}")
    else:
        settings = await get_settings(message.chat.id)

        if settings.get("welcome"):
            for u in message.new_chat_members:
                if temp.MELCOW.get('welcome'):
                    try:
                        await temp.MELCOW['welcome'].delete()
                    except Exception:
                        pass
                try:
                    temp.MELCOW['welcome'] = await message.reply_photo(
                        photo=MELCOW_PHOTO,
                        caption=script.MELCOW_ENG.format(u.mention, message.chat.title),
                        reply_markup=InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton("📌 ᴄᴏɴᴛᴀᴄᴛ ꜱᴜᴘᴘᴏʀᴛ 📌", url=OWNER_LNK)
                                ]]),parse_mode=enums.ParseMode.HTML)
                except Exception as e:
                    logger.error("Welcome photo send failed: %s", e)
        if settings.get("auto_delete"):
            await asyncio.sleep(600)
            try:
                if temp.MELCOW.get('welcome'):
                    await temp.MELCOW['welcome'].delete()
                    temp.MELCOW['welcome'] = None 
            except Exception:
                pass
               
@Client.on_message(filters.command('leave') & filters.user(ADMINS))
async def leave_a_chat(bot, message):
    if len(message.command) == 1:
        return await message.reply('Give me a chat id')
    chat = message.command[1]
    try:
        chat = int(chat)
    except Exception:
        chat = chat
    try:
        buttons = [[
                  InlineKeyboardButton("📌 ᴄᴏɴᴛᴀᴄᴛ ꜱᴜᴘᴘᴏʀᴛ 📌", url=OWNER_LNK)
                  ]]
        reply_markup=InlineKeyboardMarkup(buttons)
        await bot.send_message(
            chat_id=chat,
            text=script.LEAVE_CHAT_TXT,
            reply_markup=reply_markup,
        )

        await bot.leave_chat(chat)
        await message.reply(f"left the chat `{chat}`")
    except Exception as e:
        await message.reply(f'Error - {e}')

@Client.on_message(filters.command('disable') & filters.user(ADMINS))
async def disable_chat(bot, message):
    if len(message.command) == 1:
        return await message.reply('Give me a chat id')
    r = message.text.split(None)
    if len(r) > 2:
        reason = message.text.split(None, 2)[2]
        chat = message.text.split(None, 2)[1]
    else:
        chat = message.command[1]
        reason = "No reason Provided"
    try:
        chat_ = int(chat)
    except Exception:
        return await message.reply('Give Me A Valid Chat ID')
    cha_t = await db.get_chat(int(chat_))
    if not cha_t:
        return await message.reply("Chat Not Found In DB")
    if cha_t['is_disabled']:
        return await message.reply(f"This chat is already disabled:\nReason-<code> {cha_t['reason']} </code>")
    await db.disable_chat(int(chat_), reason)
    temp.BANNED_CHATS.append(int(chat_))
    await message.reply('Chat Successfully Disabled')
    try:
        buttons = [[
            InlineKeyboardButton('📌 ᴄᴏɴᴛᴀᴄᴛ ꜱᴜᴘᴘᴏʀᴛ 📌', url=OWNER_LNK)
        ]]
        reply_markup=InlineKeyboardMarkup(buttons)
        await bot.send_message(
            chat_id=chat_, 
            text=script.LEAVE_CHAT_TXT + f"\nReason : <code>{reason}</code>",
            reply_markup=reply_markup)
        await bot.leave_chat(chat_)
    except Exception as e:
        await message.reply(f"Error - {e}")


@Client.on_message(filters.command('enable') & filters.user(ADMINS))
async def re_enable_chat(bot, message):
    if len(message.command) == 1:
        return await message.reply('Give me a chat id')
    chat = message.command[1]
    try:
        chat_ = int(chat)
    except Exception:
        return await message.reply('Give Me A Valid Chat ID')
    sts = await db.get_chat(int(chat))
    if not sts:
        return await message.reply("Chat Not Found In DB !")
    if not sts.get('is_disabled'):
        return await message.reply('This chat is not yet disabled.')
    await db.re_enable_chat(int(chat_))
    temp.BANNED_CHATS.remove(int(chat_))
    await message.reply("Chat Successfully re-enabled")


@Client.on_message(filters.command('stats') & filters.user(ADMINS))
async def get_stats(bot, message):
    msg = await message.reply('ᴀᴄᴄᴇꜱꜱɪɴɢ ꜱᴛᴀᴛᴜꜱ ᴅᴇᴛᴀɪʟꜱ...')
    try:
        text = await _stats_text()
        await msg.edit(text, reply_markup=_stats_buttons())
    except Exception as e:
        logger.exception("Error In stats: %s", e)
        try:
            await msg.edit(f"<b>❗ Stats error:</b> <code>{e}</code>")
        except Exception:
            pass


def _stats_buttons(confirm=False):
    if confirm:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, Restart", callback_data="hs_restart_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="hs_restart_no"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="hs_refresh"),
        InlineKeyboardButton("♻️ Restart", callback_data="hs_restart"),
    ]])


async def _stats_text():
    info = await bot_health.get_db_info()
    today = await bot_health.get_today()
    text, _level = bot_health.build_text(info, today, time() - botStartTime)
    return text


def _hs_is_admin(user_id):
    return str(user_id) in {str(a) for a in ADMINS}


_hs_last_refresh = 0.0


@Client.on_callback_query(filters.regex(r"^hs_refresh$"))
async def stats_refresh(bot, query):
    global _hs_last_refresh
    if not _hs_is_admin(query.from_user.id):
        return await query.answer("Not for you!", show_alert=True)
    if time() - _hs_last_refresh < 10:
        return await query.answer("Wait a few seconds before refreshing again.")
    _hs_last_refresh = time()
    try:
        await query.answer("Refreshing...")
        text = await _stats_text()
        await query.message.edit_text(text, reply_markup=_stats_buttons())
    except MessageNotModified:
        pass
    except Exception as e:
        logger.exception("Error in stats refresh: %s", e)


@Client.on_callback_query(filters.regex(r"^hs_restart$"))
async def stats_restart_ask(bot, query):
    if not _hs_is_admin(query.from_user.id):
        return await query.answer("Not for you!", show_alert=True)
    try:
        await query.answer("Restart the bot?")
        await query.message.edit_reply_markup(_stats_buttons(confirm=True))
    except MessageNotModified:
        pass
    except Exception as e:
        logger.exception("Error in restart confirm: %s", e)


@Client.on_callback_query(filters.regex(r"^hs_restart_no$"))
async def stats_restart_cancel(bot, query):
    if not _hs_is_admin(query.from_user.id):
        return await query.answer("Not for you!", show_alert=True)
    try:
        await query.answer("Cancelled")
        await query.message.edit_reply_markup(_stats_buttons())
    except MessageNotModified:
        pass
    except Exception as e:
        logger.exception("Error in restart cancel: %s", e)


@Client.on_callback_query(filters.regex(r"^hs_restart_yes$"))
async def stats_restart_do(bot, query):
    if not _hs_is_admin(query.from_user.id):
        return await query.answer("Not for you!", show_alert=True)
    try:
        await query.answer("Restarting...")
        await query.message.edit_text("<b><i>ʙᴏᴛ ɪꜱ ʀᴇꜱᴛᴀʀᴛɪɴɢ</i></b>")
    except Exception:
        pass
    try:
        await bot_health.flush()
    except Exception:
        pass
    await asyncio.sleep(2)
    os.execl(sys.executable, sys.executable, *sys.argv)

@Client.on_message(filters.command('invite') & filters.user(ADMINS))
async def gen_invite(bot, message):
    if len(message.command) == 1:
        return await message.reply('Give me a chat id')
    chat = message.command[1]
    try:
        chat = int(chat)
    except Exception:
        return await message.reply('Give Me A Valid Chat ID')
    try:
        link = await bot.create_chat_invite_link(chat)
    except ChatAdminRequired:
        return await message.reply("Invite Link Generation Failed, Iam Not Having Sufficient Rights")
    except Exception as e:
        return await message.reply(f'Error {e}')
    await message.reply(f'Here is your Invite Link {link.invite_link}')

@Client.on_message(filters.command('ban') & filters.user(ADMINS))
async def ban_a_user(bot, message):
    if len(message.command) == 1:
        return await message.reply('Give me a user id / username')
    r = message.text.split(None)
    if len(r) > 2:
        reason = message.text.split(None, 2)[2]
        chat = message.text.split(None, 2)[1]
    else:
        chat = message.command[1]
        reason = "No reason Provided"
    try:
        chat = int(chat)
    except Exception:
        pass
    try:
        k = await bot.get_users(chat)
    except PeerIdInvalid:
        return await message.reply("This is an invalid user, make sure I have met him before.")
    except IndexError:
        return await message.reply("This might be a channel, make sure its a user.")
    except Exception as e:
        return await message.reply(f'Error - {e}')
    else:
        if str(k.id) in {str(admin) for admin in ADMINS}:
            return await message.reply(f"Nice try 😏 {k.mention} is my admin. Boss ko ban nahi kar sakta!")
        jar = await db.get_ban_status(k.id)
        if jar['is_banned']:
            return await message.reply(f"{k.mention} is already banned\nReason: {jar['ban_reason']}")
        await db.ban_user(k.id, reason)
        temp.BANNED_USERS.append(k.id)
        await message.reply(f"Successfully banned {k.mention}")


    
@Client.on_message(filters.command('unban') & filters.user(ADMINS))
async def unban_a_user(bot, message):
    if len(message.command) == 1:
        return await message.reply('Give me a user id / username')
    chat = message.command[1]
    try:
        chat = int(chat)
    except Exception:
        pass
    try:
        k = await bot.get_users(chat)
    except PeerIdInvalid:
        return await message.reply("This is an invalid user, make sure ia have met him before.")
    except IndexError:
        return await message.reply("Thismight be a channel, make sure its a user.")
    except Exception as e:
        return await message.reply(f'Error - {e}')
    else:
        jar = await db.get_ban_status(k.id)
        if not jar['is_banned']:
            return await message.reply(f"{k.mention} is not yet banned.")
        await db.remove_ban(k.id)
        temp.BANNED_USERS.remove(k.id)
        await message.reply(f"Successfully unbanned {k.mention}")


    
@Client.on_message(filters.command('users') & filters.user(ADMINS))
async def list_users(bot, message):
    dreamxbotz = await message.reply('Getting List Of Users')
    users = await db.get_all_users()
    out = "Users Saved In DB Are:\n\n"
    async for user in users:
        out += f"<a href=tg://user?id={user['id']}>{user['name']}</a>"
        if user['ban_status']['is_banned']:
            out += '( Banned User )'
        out += '\n'
    try:
        await dreamxbotz.edit_text(out)
    except MessageTooLong:
        with open('users.txt', 'w+') as outfile:
            outfile.write(out)
        await message.reply_document('users.txt', caption="List Of Users")

@Client.on_message(filters.command('chats') & filters.user(ADMINS))
async def list_chats(bot, message):
    dreamxbotz = await message.reply('Getting List Of chats')
    chats = await db.get_all_chats()
    out = "Chats Saved In DB Are:\n\n"
    async for chat in chats:
        out += f"**Title:** `{chat['title']}`\n**- ID:** `{chat['id']}`"
        if chat['chat_status']['is_disabled']:
            out += '( Disabled Chat )'
        out += '\n'
    try:
        await dreamxbotz.edit_text(out)
    except MessageTooLong:
        with open('chats.txt', 'w+') as outfile:
            outfile.write(out)
        await message.reply_document('chats.txt', caption="List Of Chats")


@Client.on_message(filters.command('group_cmd'))
async def group_commands(client, message):
    await message.reply_text(script.GROUP_CMD, disable_web_page_preview=True)

@Client.on_message(filters.command('admin_cmd') & filters.user(ADMINS))
async def admin_commands(client, message):
    await message.reply_text(script.ADMIN_CMD, disable_web_page_preview=True)
    

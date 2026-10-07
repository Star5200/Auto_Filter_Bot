import time
import logging
from types import SimpleNamespace

from pyrogram import Client, enums
from pyrogram.filters import create
from pyrogram.types import ChatMemberUpdated, ChatJoinRequest

from database.users_chats_db import db
from info import AUTH_CHANNELS, AUTH_REQ_CHANNELS
from utils import temp, get_settings, is_subscribed, is_req_subscribed

logger = logging.getLogger(__name__)

PENDING_TTL = 1800  # user ne itni der (30 min) ke andar join kiya tabhi file apne aap jayegi
_JOINED = (
    enums.ChatMemberStatus.MEMBER,
    enums.ChatMemberStatus.ADMINISTRATOR,
    enums.ChatMemberStatus.OWNER,
)


class _FakeMessage:
    """/start handler ko chalane ke liye nakli message (reply user ko seedha PM mein jata hai)."""

    def __init__(self, client, user, payload):
        self._c = client
        self.from_user = user
        self.command = ["start", payload]
        self.text = f"/start {payload}"
        self.id = 0
        self.chat = SimpleNamespace(
            id=user.id, title=None, username=user.username, type=enums.ChatType.PRIVATE
        )

    async def reply_text(self, text, *args, **kwargs):
        kwargs.pop("quote", None)
        return await self._c.send_message(self.from_user.id, text, **kwargs)

    reply = reply_text

    async def reply_photo(self, photo, *args, **kwargs):
        kwargs.pop("quote", None)
        return await self._c.send_photo(self.from_user.id, photo, **kwargs)

    async def react(self, *args, **kwargs):
        return None

    async def delete(self, *args, **kwargs):
        return None


async def _process(client, user):
    """User ke liye pending file hai aur ab saare channels join ho gaye hain to file bhej do."""
    pending = getattr(temp, "PENDING_FSUB", None)
    if not pending:
        return
    item = pending.get(user.id)
    if not item:
        return
    payload, sent, ts = item
    if time.time() - ts > PENDING_TTL:
        pending.pop(user.id, None)
        return

    # kya ab saare required channels join ho gaye?
    try:
        grp_id = int(payload.split("_")[1])
    except Exception:
        grp_id = 0
    settings = await get_settings(grp_id)
    fsub_channels = list(dict.fromkeys((settings.get("fsub", []) if settings else []) + AUTH_CHANNELS))
    btn = []
    if fsub_channels:
        btn += await is_subscribed(client, user.id, fsub_channels)
    if AUTH_REQ_CHANNELS:
        btn += await is_req_subscribed(client, user.id, AUTH_REQ_CHANNELS)
    if btn:
        return  # abhi koi channel baaki hai

    pending.pop(user.id, None)
    try:
        await sent.delete()  # purana "join karo / try again" message hata do
    except Exception:
        pass

    from plugins.commands import start  # wahi purana flow (verify, caption, auto-delete sab)
    await start(client, _FakeMessage(client, user, payload))


# 1) Normal fsub (AUTH_CHANNELS): user ne channel join kiya
@Client.on_chat_member_updated(group=3)
async def auto_send_after_join(client, update: ChatMemberUpdated):
    try:
        new = update.new_chat_member
        if not new or new.status not in _JOINED:
            return
        await _process(client, new.user)
    except Exception as e:
        logger.error("fsub auto-send (join) error: %s", repr(e))


# 2) Request fsub (AUTH_REQ_CHANNELS): user ne join request bheji
def _is_req_channel(_, __, update):
    return update.chat.id in AUTH_REQ_CHANNELS


@Client.on_chat_join_request(create(_is_req_channel), group=3)
async def auto_send_after_request(client, req: ChatJoinRequest):
    try:
        await db.add_join_req(req.from_user.id, req.chat.id)  # pehle record pakka kar lo
        await _process(client, req.from_user)
    except Exception as e:
        logger.error("fsub auto-send (request) error: %s", repr(e))

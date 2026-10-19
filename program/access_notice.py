"""Friendly notices for non-admin users in configured group chats.

Existing command/callback handlers intentionally filter out non-admins. These
lower-priority handlers provide feedback without changing those access checks.
"""
import re
import logging

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ALLOWED_GROUPS, BOT_USERNAME, COMMAND_PREFIXES
from driver.admins import get_administrators

log = logging.getLogger(__name__)
CONTACT_URL = "https://t.me/faridahmad2003"
CONTACT_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("📩 Contact @faridahmad2003", url=CONTACT_URL)]]
)
NOTICE = (
    "🔒 **Only group administrators can use this action.**\n\n"
    "Interested in becoming an admin? Contact @faridahmad2003 using the button below."
)
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM)


def _is_group_allowed(chat):
    return bool(chat and chat.type in GROUP_TYPES and chat.id in ALLOWED_GROUPS)


def _is_command(message):
    raw = message.text or message.caption or ""
    if not raw:
        return False
    # Prefixes are usually '/' but respect the project's configured prefixes.
    for prefix in COMMAND_PREFIXES:
        if raw.startswith(prefix):
            first = raw.split(None, 1)[0][len(prefix):]
            if "@" in first:
                command_name, target = first.split("@", 1)
                if target.lower() != BOT_USERNAME.lower():
                    return False
            else:
                command_name = first
            return bool(re.fullmatch(r"[A-Za-z0-9_]+", command_name))
    return False


async def _is_admin(client, chat, user_id):
    try:
        return user_id in await get_administrators(chat)
    except Exception:
        log.exception("Could not verify group admin status for access notice")
        return False


@Client.on_message(filters.group & filters.text, group=1)
async def non_admin_command_notice(client, message):
    user = message.from_user
    if not user or not _is_command(message) or not _is_group_allowed(message.chat):
        return
    if await _is_admin(client, message.chat, user.id):
        return
    try:
        await message.reply_text(NOTICE, reply_markup=CONTACT_KEYBOARD, disable_web_page_preview=True)
    except Exception:
        log.exception("Failed to send non-admin command notice")


@Client.on_callback_query(group=1)
async def non_admin_button_notice(client, query):
    user = query.from_user
    message = query.message
    chat = getattr(message, "chat", None)
    if not user or not _is_group_allowed(chat):
        return
    if await _is_admin(client, chat, user.id):
        return
    # A callback alert is the requested popup. Telegram popups cannot contain
    # inline buttons, so also attach the contact button to a chat message.
    try:
        await query.answer(
            "Only group admins can use this action. Contact @faridahmad2003 for admin access.",
            show_alert=True,
        )
    except Exception:
        pass
    try:
        await message.reply_text(NOTICE, reply_markup=CONTACT_KEYBOARD, disable_web_page_preview=True)
    except Exception:
        # The original message may be inaccessible/deleted; the popup still helps.
        log.debug("Could not post contact button for non-admin callback", exc_info=True)

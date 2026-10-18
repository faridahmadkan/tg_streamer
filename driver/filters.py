import re
from time import monotonic
from typing import List, Union

from pyrogram import filters
from pyrogram.enums import ChatType

from config import ALLOWED_GROUPS, BOT_ADMINS, BOT_USERNAME, COMMAND_PREFIXES, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW, SUDO_USERS
from driver.queues import TOPIC_LOCK
from driver.admins import get_administrators


# /topic itself must always get through, locked or not — otherwise a chat that
# locks the "wrong" topic (or the one someone later deletes) can't undo it.
#
# This deliberately does NOT use pyrogram's filters.command(): that filter
# mutates message.command as a side effect of every check (resets it to None,
# then repopulates it only on a match) — and since this check runs as part of
# other_filters, which every handler's filter chain ANDs in *after* its own
# command([...]) filter already matched and set message.command correctly, it
# would clobber that back to None before the handler ever sees it (every
# command handler crashed with "NoneType has no len()" in any topic-locked
# chat). A plain, non-mutating text match avoids that entirely.
_topic_cmd_re = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in COMMAND_PREFIXES) + r")"
    rf"topic(?:@{re.escape(BOT_USERNAME)})?(?:\s|$)",
    re.IGNORECASE,
)


def _is_topic_command(m):
    text = m.text or m.caption
    return bool(text and _topic_cmd_re.match(text))


async def _topic_ok(_, client, m):
    if not TOPIC_LOCK or m.chat is None or m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM):
        return True
    locked = TOPIC_LOCK.get(m.chat.id)
    if locked is None:
        return True
    if _is_topic_command(m):
        return True
    thread_id = m.message_thread_id if getattr(m, "topic_message", False) else None
    return thread_id == locked


topic_ok = filters.create(_topic_ok)

other_filters = filters.group & ~filters.via_bot & ~filters.forwarded & topic_ok
other_filters2 = (
    filters.private & ~filters.via_bot & ~filters.forwarded
)


# Per-user sliding-window command rate limit. Excess commands are silently
# dropped (the handler simply doesn't fire) so a spammer can't flood the bot.
_cmd_history = {}  # user_id -> [recent command timestamps]


async def _rate_ok(_, __, m):
    if not RATE_LIMIT_MAX:
        return True
    user = m.from_user
    if user is None or user.id in SUDO_USERS:
        return True
    now = monotonic()
    cutoff = now - RATE_LIMIT_WINDOW
    hist = _cmd_history.setdefault(user.id, [])
    while hist and hist[0] < cutoff:
        hist.pop(0)
    if len(hist) >= RATE_LIMIT_MAX:
        return False
    hist.append(now)
    return True


rate_limit = filters.create(_rate_ok)


async def _access_ok(_, client, update):
    """Only approved groups, and only their Telegram administrators, may use commands/buttons.

    Private commands are reserved for IDs in BOT_ADMINS (including /start).
    """
    user = getattr(update, "from_user", None)
    if user is None:
        return False

    # Message commands and callback queries both expose the originating chat.
    message = getattr(update, "message", None)
    chat = getattr(update, "chat", None) or getattr(message, "chat", None)
    if chat is None:
        return False

    if chat.type == ChatType.PRIVATE:
        return user.id in BOT_ADMINS

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM):
        return False
    if chat.id not in ALLOWED_GROUPS:
        return False

    try:
        administrators = await get_administrators(chat)
        return user.id in administrators
    except Exception:
        # Fail closed if Telegram cannot confirm admin status.
        return False


access_filter = filters.create(_access_ok)


def command(commands: Union[str, List[str]]):
    return filters.command(commands, COMMAND_PREFIXES) & rate_limit & access_filter

from pyrogram.types import LinkPreviewOptions
import asyncio

from config import BOT_USERNAME
from driver.decorators import errors, errors_cb
from driver.filters import command, other_filters, access_filter
from driver.queues import QUEUE, add_to_queue, drop_if_live, set_active_thread
from driver.clients import call_py
from config import MAX_QUEUE_SIZE
from driver.utils import control_panel, media_audio, ensure_can_play
from driver.design.thumbnail import thumb
from driver.design.chatname import CHAT_TITLE
from program.music import ytdl as _audio_dl, start_playback_progress
from program.music import ytsearch_all
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# chat_id -> [(title, url, duration), ...] from the last /search
SEARCH = {}


def _results(query, n=6):
    try:
        data = ytsearch_all(query, n)
        # Keep artwork and provider metadata so selected search results can use
        # the same now-playing card as a regular /play request.
        return [(item[0], item[1], item[2], item[3], item[4]) for item in data]
    except Exception:
        return []


@Client.on_message(command(["search", f"search@{BOT_USERNAME}", "musicsearch", "ms"]) & other_filters)
@errors
async def search_cmd(c: Client, m: Message):
    if len(m.command) < 2:
        return await m.reply("» Usage: /search song name or supported audio URL", parse_mode=None)
    query = m.text.split(None, 1)[1]
    loading = await m.reply("🔍 **Searching…**")
    res = await asyncio.to_thread(_results, query)
    if not res:
        return await loading.edit("🔍 no results found.")
    SEARCH[m.chat.id] = res
    rows = []
    for i, (title, _url, dur, _thumbnail, provider) in enumerate(res):
        label = f"🎵 {title[:30]}"
        if dur:
            label += f" · {dur}"
        rows.append([InlineKeyboardButton(label[:58], callback_data=f"sa:{i}")])
    rows.append([InlineKeyboardButton("🗑 Close", callback_data="cls")])
    await loading.edit(
        f"🔍 **Results for** `{query[:50]}`\nTap a result to play audio:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _play_choice(c, query, video):
    res = SEARCH.get(query.message.chat.id)
    try:
        choice = res[int(query.data.split(":")[1])]
        title, url, duration, thumbnail, provider = choice
    except (TypeError, IndexError, ValueError):
        return await query.answer("results expired — search again", show_alert=True)
    chat_id = query.message.chat.id
    await query.answer("preparing audio…")
    if video:
        return await query.answer("Video search is not enabled in this music search.", show_alert=True)
    if not await ensure_can_play(c, query.message):
        return
    status = await query.edit_message_text(f"📥 **Preparing** `{title[:60]}`…")
    ok, path = await _audio_dl("bestaudio", url, status)
    if ok == 0:
        return await status.edit_text(f"❌ **Could not download this track.**\n\n`{str(path)[:700]}`", parse_mode=None)

    drop_if_live(chat_id)  # radio/IPTV never hold a real queue slot
    set_active_thread(chat_id, getattr(query.message, "message_thread_id", None))
    if chat_id in QUEUE:
        pos = add_to_queue(chat_id, title[:70], path, url, "Audio", 0)
        if pos == -1:
            return await status.edit_text(f"🚫 queue is full (max {MAX_QUEUE_SIZE}).")
        try:
            await status.delete()
        except Exception:
            pass
        requester = f"[{query.from_user.first_name}](tg://user?id={query.from_user.id})" if query.from_user else "Unknown"
        try:
            ctitle = await CHAT_TITLE(query.message.chat.title or "Telegram Chat")
            image = await thumb(thumbnail, title, query.from_user.id if query.from_user else 0, ctitle)
        except Exception:
            image = None
        caption = (f"💡 **Track added to queue »** `{pos}`\n\n🏷 **Name:** [{title[:70]}]({url})\n"
                   f"**⏱ Duration:** `{duration or 'Unknown'}`\n🎧 **Request by:** {requester}")
        if image:
            await c.send_photo(chat_id, photo=image, caption=caption, reply_markup=control_panel,
                               message_thread_id=getattr(query.message, "message_thread_id", None))
        else:
            await c.send_message(chat_id, caption, reply_markup=control_panel,
                                 message_thread_id=getattr(query.message, "message_thread_id", None),
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
        return

    try:
        await call_py.play(chat_id, media_audio(path))
        add_to_queue(chat_id, title[:70], path, url, "Audio", 0)
    except Exception as exc:
        return await status.edit_text(f"🚫 **Could not start playback.**\n\n`{str(exc)[:700]}`", parse_mode=None)

    try:
        await status.delete()
    except Exception:
        pass
    requester = f"[{query.from_user.first_name}](tg://user?id={query.from_user.id})" if query.from_user else "Unknown"
    try:
        ctitle = await CHAT_TITLE(query.message.chat.title or "Telegram Chat")
        image = await thumb(thumbnail, title, query.from_user.id if query.from_user else 0, ctitle)
    except Exception:
        image = None
    from program.music import _progress_caption
    caption = _progress_caption(title, url, duration, requester, 0)
    thread_id = getattr(query.message, "message_thread_id", None)
    if image:
        status_message = await c.send_photo(chat_id, photo=image, caption=caption, reply_markup=control_panel, message_thread_id=thread_id)
    else:
        status_message = await c.send_message(chat_id, caption, reply_markup=control_panel, message_thread_id=thread_id,
                                              link_preview_options=LinkPreviewOptions(is_disabled=True))
    await start_playback_progress(chat_id, status_message, title[:70], url, duration, requester, control_panel)


@Client.on_callback_query(filters.regex(r"^sa:") & access_filter)
@errors_cb
async def search_audio_cb(c: Client, query: CallbackQuery):
    await _play_choice(c, query, video=False)



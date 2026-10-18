from pyrogram.types import LinkPreviewOptions
import asyncio

from config import BOT_USERNAME
from driver.decorators import errors, errors_cb
from driver.filters import command, other_filters, access_filter
from driver.queues import QUEUE, add_to_queue, drop_if_live, set_active_thread
from driver.clients import call_py
from config import MAX_QUEUE_SIZE
from driver.utils import control_panel, media_audio, ensure_can_play
from program.music import ytdl as _audio_dl
from program.music import ytsearch_all
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# chat_id -> [(title, url, duration), ...] from the last /search
SEARCH = {}


def _results(query, n=6):
    try:
        data = ytsearch_all(query, n)
        return [(item[0], item[1], item[2], item[4]) for item in data]
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
    for i, (title, _url, dur, provider) in enumerate(res):
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
        title, url, _dur = choice[:3]
    except (TypeError, IndexError, ValueError):
        return await query.answer("results expired — search again", show_alert=True)
    chat_id = query.message.chat.id
    await query.answer("loading…")
    if not await ensure_can_play(c, query.message):
        return
    await query.edit_message_text(f"📥 preparing `{title[:50]}`…")
    if video:
        ok, path = await _video_dl(url, query.message)
        if ok == 0:
            return await query.edit_message_text(f"❌ download failed:\n`{str(path)[:150]}`")
        path = await prepare_for_stream(path, query.message)
        stream, typ, Q = media_video(path, 720), "Video", 720
    else:
        ok, path = await _audio_dl("bestaudio", url, query.message)
        if ok == 0:
            return await query.edit_message_text(f"❌ download failed:\n`{str(path)[:150]}`")
        stream, typ, Q = media_audio(path), "Audio", 0
    drop_if_live(chat_id)  # radio/IPTV never hold a real queue slot
    set_active_thread(chat_id, getattr(query.message, "message_thread_id", None))
    if chat_id in QUEUE:
        pos = add_to_queue(chat_id, title[:70], path, url, typ, Q)
        if pos == -1:
            return await query.edit_message_text(f"🚫 queue is full (max {MAX_QUEUE_SIZE}).")
        return await query.edit_message_text(
            f"💡 **Queued #{pos}:** [{title[:50]}]({url}) · `{typ}`",
            reply_markup=control_panel, link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    try:
        await call_py.play(chat_id, stream)
        add_to_queue(chat_id, title[:70], path, url, typ, Q)
        await query.edit_message_text(
            f"🎧 **Now playing:** [{title[:50]}]({url}) · `{typ}`",
            reply_markup=control_panel, link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as e:
        await query.edit_message_text(f"🚫 error: `{e}`")


@Client.on_callback_query(filters.regex(r"^sa:") & access_filter)
@errors_cb
async def search_audio_cb(c: Client, query: CallbackQuery):
    await _play_choice(c, query, video=False)



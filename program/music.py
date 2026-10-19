

import os
import re
import asyncio
import subprocess
import json
from pathlib import Path
from time import time

import logging

from config import ASSISTANT_NAME, BOT_USERNAME, COOKIES_FILE, IMG_1, IMG_2, MAX_QUEUE_SIZE, PROXY_URL, SPONSORBLOCK_REMOVE, SUDO_USERS
from driver.design.thumbnail import thumb
from driver.design.chatname import CHAT_TITLE
from driver.decorators import errors
from driver.filters import command, other_filters
from driver.queues import QUEUE, add_to_queue, drop_if_live, set_active_thread
from driver.clients import call_py, user
from driver.utils import get_assistant_member, bash, make_progress, control_panel, media_audio, drop_stale_queue
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserAlreadyParticipant, UserNotParticipant, PeerIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message


log = logging.getLogger(__name__)

# Live now-playing progress cards. One updater per chat; it stops when the
# current queue item changes or the message can no longer be edited.
_PLAYBACK_PROGRESS_TASKS = {}

def _duration_seconds(value):
    """Parse common duration forms (mm:ss, hh:mm:ss, or seconds)."""
    if value is None:
        return 0
    try:
        if isinstance(value, (int, float)):
            return max(0, int(value))
        value = str(value).strip()
        if not value:
            return 0
        if ":" in value:
            parts = [int(part) for part in value.split(":")]
            total = 0
            for part in parts:
                total = total * 60 + part
            return max(0, total)
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0

def _clock(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

def _progress_caption(title, url, duration, requester, position):
    total = _duration_seconds(duration)
    if total > 0:
        ratio = min(1.0, max(0.0, position / total))
        filled = min(12, int(ratio * 12))
        bar = "━" * filled + ("●" if filled < 12 else "") + "─" * max(0, 11 - filled)
        timeline = f"`{_clock(position)}` {bar} `{_clock(total)}`"
    else:
        # Duration is not always supplied by a provider; still show elapsed time.
        bar = "━" * (1 + (position // 10) % 10) + "●"
        timeline = f"`{_clock(position)}` {bar}"
    return (f"🏷 **Name:** [{title[:70]}]({url})\n"
            f"⏱ **Duration:** `{_clock(total) if total else (str(duration) or 'Unknown')}`\n"
            f"{timeline}\n"
            f"💡 **Status:** `Playing`\n🎧 **Request by:** {requester}\n📹 **Stream type:** `Music`")

async def start_playback_progress(chat_id, message, title, url, duration, requester, keyboard):
    """Refresh the now-playing caption with an elapsed/total progress bar."""
    old = _PLAYBACK_PROGRESS_TASKS.get(chat_id)
    if old and not old.done():
        old.cancel()

    async def update():
        try:
            while True:
                await asyncio.sleep(8)
                queue = QUEUE.get(chat_id) or []
                if not queue or queue[0][0] != title or queue[0][2] != url:
                    return
                try:
                    position = int(await call_py.time(chat_id))
                except Exception:
                    continue
                caption = _progress_caption(title, url, duration, requester, position)
                try:
                    if getattr(message, "photo", None):
                        await message.edit_caption(caption=caption, reply_markup=keyboard)
                    else:
                        await message.edit_text(caption, reply_markup=keyboard, link_preview_options=LinkPreviewOptions(is_disabled=True))
                except Exception as exc:
                    log.debug("Playback progress updater stopped: %s", exc)
                    return
                total = _duration_seconds(duration)
                if total and position >= total:
                    return
        except asyncio.CancelledError:
            return
        finally:
            if _PLAYBACK_PROGRESS_TASKS.get(chat_id) is asyncio.current_task():
                _PLAYBACK_PROGRESS_TASKS.pop(chat_id, None)

    _PLAYBACK_PROGRESS_TASKS[chat_id] = asyncio.create_task(update())

_PROVIDER_FILE = Path("data/music_provider.json")
_PROVIDER_OPTIONS = {
    "auto": "Auto (YouTube + SoundCloud + Internet Archive)",
    "youtube": "YouTube search",
    "soundcloud": "SoundCloud search",
    "archive": "Internet Archive (public/authorized audio)",
    "bandcamp": "Bandcamp (direct links)",
    "audiomack": "Audiomack (direct links)",
    "mixcloud": "Mixcloud (direct links)",
}


def _get_provider():
    try:
        value = json.loads(_PROVIDER_FILE.read_text(encoding="utf-8")).get("provider", "auto")
        return value if value in _PROVIDER_OPTIONS else "auto"
    except Exception:
        return "auto"


def _set_provider(value):
    if value not in _PROVIDER_OPTIONS:
        return False
    try:
        _PROVIDER_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROVIDER_FILE.write_text(json.dumps({"provider": value}), encoding="utf-8")
        return True
    except OSError as exc:
        log.warning("Could not persist music provider: %s", exc)
        return False

def _ytdl_site_flags():
    """Build yt-dlp flags shared by supported sites."""
    flags = ["--socket-timeout", "60", "--retries", "5", "--fragment-retries", "5", "--extractor-retries", "3", "--file-access-retries", "3"]
    if COOKIES_FILE:
        flags += ["--cookies", COOKIES_FILE]
    if PROXY_URL:
        flags += ["--proxy", PROXY_URL]
    return flags


_YOUTUBE_RE = re.compile(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)
_AUDIO_HOSTS = (
    "soundcloud.com", "bandcamp.com", "audiomack.com", "mixcloud.com",
    "archive.org", "www.archive.org", "youtube.com", "www.youtube.com", "youtu.be",
)


def _internet_archive_search(query: str, limit: int = 5):
    """Search Internet Archive's public audio catalog and return downloadable audio items."""
    from urllib.parse import urlencode, quote
    from urllib.request import Request, urlopen

    safe_query = query.replace('"', ' ').strip()[:100]
    params = urlencode({
        "q": f'mediatype:audio AND (title:"{safe_query}" OR creator:"{safe_query}" OR description:"{safe_query}")',
        "fl[]": ["identifier", "title", "creator"],
        "rows": max(1, min(limit, 8)),
        "output": "json",
    }, doseq=True)
    request = Request("https://archive.org/advancedsearch.php?" + params,
                      headers={"User-Agent": "KhanStreamer/1.0 (audio catalog search)"})
    results = []
    try:
        with urlopen(request, timeout=8) as response:
            docs = json.loads(response.read().decode("utf-8")).get("response", {}).get("docs", [])
        for item in docs:
            identifier = item.get("identifier")
            if not identifier:
                continue
            meta_req = Request(f"https://archive.org/metadata/{quote(identifier)}",
                               headers={"User-Agent": "KhanStreamer/1.0"})
            try:
                with urlopen(meta_req, timeout=8) as response:
                    files = json.loads(response.read().decode("utf-8")).get("files", [])
            except Exception as exc:
                log.debug("Archive metadata failed for %s: %s", identifier, exc)
                continue
            audio = next((f for f in files if str(f.get("name", "")).lower().endswith(
                (".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav"))
                and not f.get("private") and not f.get("source", "").startswith("original")), None)
            if not audio:
                audio = next((f for f in files if str(f.get("name", "")).lower().endswith(
                    (".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav")) and not f.get("private")), None)
            if not audio:
                continue
            title = item.get("title") or identifier
            file_name = audio.get("name", "")
            url = f"https://archive.org/download/{quote(identifier)}/{quote(file_name)}"
            results.append([title, url, str(audio.get("length", "")), "", "Internet Archive"])
            if len(results) >= limit:
                break
    except Exception as exc:
        log.warning("Internet Archive search failed: %s", exc)
    return results


def ytsearch(query: str):
    """Compatibility helper: return the first non-YouTube music result."""
    results = ytsearch_all(query, 1)
    if not results:
        return 0
    title, url, duration, thumbnail, _provider = results[0]
    return [title, url, duration, thumbnail]


def ytsearch_all(query: str, limit: int = 5):
    """Search supported music sources; YouTube is intentionally excluded."""
    q = (query or "").strip()
    if not q:
        return []
    candidates, seen = [], set()

    def add(title, url, duration="", thumbnail="", provider=""):
        if not url:
            return
        url = str(url).strip()
        if not url.startswith(("https://", "http://")) or url in seen:
            return
        seen.add(url)
        candidates.append([title or q, url, duration or "", thumbnail or "", provider])

    if re.match(r"https?://", q, re.I):
        if not any(host in q.lower().split("/")[2] for host in _AUDIO_HOSTS):
            log.warning("Direct music URL is not from an enabled provider: %s", q[:160])
            return []
        try:
            proc = subprocess.run(
                ["yt-dlp", "--no-warnings", "--skip-download", "--no-playlist", *_ytdl_site_flags(),
                 "--print", "%(title)s\x1f%(duration_string)s\x1f%(thumbnail)s", q],
                capture_output=True, text=True, timeout=25,
            )
            fields = proc.stdout.strip().split("\x1f")
            title = fields[0] if fields and fields[0] else q
            duration = fields[1] if len(fields) > 1 else ""
            thumbnail = fields[2] if len(fields) > 2 else ""
            host = q.lower().split("/")[2]
            provider = next((name for domain, name in [
                ("soundcloud.com", "SoundCloud"), ("bandcamp.com", "Bandcamp"),
                ("audiomack.com", "Audiomack"), ("mixcloud.com", "Mixcloud"),
                ("archive.org", "Internet Archive"), ("youtube.com", "YouTube"), ("youtu.be", "YouTube")
            ] if domain in host), "Audio provider")
            add(title, q, duration, thumbnail, provider)
        except Exception as exc:
            log.warning("Direct provider metadata lookup failed: %s", exc)
        return candidates

    selected = _get_provider()
    if selected in ("auto", "youtube"):
        try:
            # yt-dlp's search extractor supports YouTube and many compatible search backends.
            proc = subprocess.run(
                ["yt-dlp", "--no-warnings", "--skip-download", "--flat-playlist", "--dump-single-json",
                 *_ytdl_site_flags(), "--socket-timeout", "10", "--retries", "1", "--extractor-retries", "1",
                 f"ytsearch{limit}:{q}"],
                capture_output=True, text=True, timeout=14,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                for item in data.get("entries") or [data]:
                    url = item.get("webpage_url") or item.get("url")
                    if url and not str(url).startswith(("http://", "https://")):
                        continue
                    add(item.get("title"), url, item.get("duration_string") or item.get("duration") or "",
                        item.get("thumbnail") or "", "YouTube")
        except Exception as exc:
            log.debug("YouTube search unavailable: %s", exc)

    if selected in ("auto", "soundcloud") and len(candidates) < limit:
        try:
            proc = subprocess.run(
                ["yt-dlp", "--no-warnings", "--skip-download", "--flat-playlist", "--dump-single-json",
                 *_ytdl_site_flags(), f"scsearch{max(1, limit - len(candidates))}:{q}"],
                capture_output=True, text=True, timeout=22,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                for item in data.get("entries") or [data]:
                    url = item.get("webpage_url") or item.get("url")
                    if url and not str(url).startswith(("http://", "https://")):
                        continue
                    add(item.get("title"), url, item.get("duration_string") or item.get("duration") or "",
                        item.get("thumbnail") or "", "SoundCloud")
            else:
                log.warning("SoundCloud search failed: %s", (proc.stderr or "")[-250:])
        except Exception as exc:
            log.warning("SoundCloud search error: %s", exc)

    if selected in ("auto", "archive") and len(candidates) < limit:
        for item in _internet_archive_search(q, limit - len(candidates)):
            add(*item)

    if selected in ("bandcamp", "audiomack", "mixcloud") and not candidates:
        log.info("%s supports direct track links; keyword search is not available through yt-dlp", _PROVIDER_OPTIONS[selected])
    return candidates[:limit]


async def _download_provider_audio(link: str):
    """Download supported provider audio locally before passing it to NTgCalls."""
    started_at = time()
    downloads = Path("downloads")
    downloads.mkdir(parents=True, exist_ok=True)
    host = link.lower().split("/")[2] if "://" in link else "unknown"
    log.info("Downloading provider audio locally: host=%s url=%s", host, link[:160])
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--no-warnings", "--no-playlist", "--no-part", "--newline",
            *_ytdl_site_flags(), "-f", "bestaudio/best",
            "-o", str(downloads / "%(extractor)s-%(id)s.%(ext)s"),
            "--print", "after_move:filepath", link,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        log.warning("Provider audio download timed out after 120s: %s", link[:160])
        proc.kill()
        await proc.wait()
        return 0, "Audio download timed out after 120 seconds."
    except Exception:
        log.exception("Could not start provider audio download")
        return 0, "Could not start audio downloader."
    elapsed = time() - started_at
    lines = [line.strip() for line in stdout.decode(errors="ignore").splitlines() if line.strip()]
    local_path = Path(lines[-1]) if lines else None
    if proc.returncode == 0 and local_path and local_path.is_file() and local_path.stat().st_size > 16_000:
        log.info("Provider audio downloaded locally in %.1fs (%s bytes): %s", elapsed, local_path.stat().st_size, local_path)
        return 1, str(local_path.resolve())
    err = stderr.decode(errors="ignore")[-700:]
    log.warning("Provider download failed (exit=%s, %.1fs): %s", proc.returncode, elapsed, err or "no output file")
    return 0, err or "Provider did not produce a valid audio file."


async def ytdl(format: str, link: str, status_msg=None):
    return await _download_provider_audio(link)


async def find_and_download_music(query: str, status_msg=None):
    """Search supported non-YouTube catalogs and try candidates until one downloads."""
    # Search providers in a worker thread so a slow external search cannot block Telegram updates.
    log.info("Starting music search for query=%r", query[:120])
    candidates = await asyncio.to_thread(ytsearch_all, query, 5)
    log.info("Music search returned %d candidate(s)", len(candidates))
    if not candidates:
        return None, "No results found on SoundCloud or Internet Archive. Try a direct SoundCloud, Bandcamp, Audiomack, Mixcloud, or Internet Archive audio link."
    errors = []
    for index, candidate in enumerate(candidates, start=1):
        title, url, duration, thumbnail, provider = candidate
        started_at = time()
        log.info("Candidate %d/%d: provider=%s title=%r url=%s", index, len(candidates), provider, title[:100], url[:180])
        try:
            if status_msg is not None:
                await status_msg.edit(f"🔎 **Trying {provider or 'music provider'}** ({index}/{len(candidates)})…\n\n🏷 `{title[:70]}`")
            # A single extractor must never leave the request stuck indefinitely.
            attempt_timeout = 125
            try:
                ok, result = await asyncio.wait_for(
                    ytdl("bestaudio/best", url, status_msg), timeout=attempt_timeout
                )
            except asyncio.TimeoutError:
                ok, result = 0, f"Provider attempt timed out after {attempt_timeout} seconds."
                log.warning("Candidate timed out after %ss: provider=%s url=%s", attempt_timeout, provider, url[:180])
            elapsed = time() - started_at
            if ok:
                log.info("Candidate resolved successfully in %.1fs: provider=%s", elapsed, provider)
                return {"title": title, "url": url, "duration": duration,
                        "thumbnail": thumbnail, "provider": provider, "media": result}, None
            log.warning("Candidate failed after %.1fs: provider=%s reason=%s", elapsed, provider, str(result)[-300:])
            errors.append(f"{provider or 'provider'}: {str(result)[-180:]}")
        except Exception as exc:
            log.exception("Unexpected error resolving candidate from provider=%s", provider)
            errors.append(f"{provider or 'provider'}: {str(exc)[-180:]}")
    return None, "All providers failed. " + " | ".join(errors[-3:])


@Client.on_message(command(["provider", f"provider@{BOT_USERNAME}"]))
@errors
async def provider_command(c: Client, m: Message):
    if not m.from_user or m.from_user.id not in SUDO_USERS:
        return await m.reply_text("⛔ Only configured bot admins can change the music provider.")
    current = _get_provider()
    rows = [[InlineKeyboardButton(("✅ " if key == current else "") + label,
                                  callback_data=f"musicprovider:{key}")]
            for key, label in _PROVIDER_OPTIONS.items()]
    await m.reply_text(
        f"🎵 **Music provider settings**\\n\\nCurrent: **{_PROVIDER_OPTIONS[current]}**\\n"
        "SoundCloud and Internet Archive support keyword search. Bandcamp, Audiomack, and Mixcloud accept direct links.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


@Client.on_callback_query(filters.regex(r"^musicprovider:"))
@errors
async def provider_callback(c, query):
    if not query.from_user or query.from_user.id not in SUDO_USERS:
        return await query.answer("Only configured bot admins can change the provider.", show_alert=True)
    value = query.data.split(":", 1)[1]
    if value not in _PROVIDER_OPTIONS:
        return await query.answer("Unknown provider.", show_alert=True)
    if not _set_provider(value):
        return await query.answer("Could not save provider; check filesystem permissions.", show_alert=True)
    rows = [[InlineKeyboardButton(("✅ " if key == value else "") + label,
                                  callback_data=f"musicprovider:{key}")]
            for key, label in _PROVIDER_OPTIONS.items()]
    try:
        await query.edit_message_text(
            f"🎵 **Music provider settings**\\n\\nCurrent: **{_PROVIDER_OPTIONS[value]}**\\n"
            "SoundCloud and Internet Archive support keyword search; Bandcamp, Audiomack, and Mixcloud accept direct links.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        pass
    await query.answer(f"Provider set to {_PROVIDER_OPTIONS[value]}")


@Client.on_message(command(["play", f"play@{BOT_USERNAME}"]) & other_filters)
@errors
async def play(c: Client, m: Message):
    # snapshot before any await: pyrogram's message-cache can mutate .command
    # back to None on a cached Message once another handler's filter check
    # runs against it concurrently, which happens easily once we start
    # awaiting below (a bare `m.command` read later crashed with
    # "TypeError: object of type 'NoneType' has no len()")
    cmd_args = m.command
    await m.delete()
    replied = m.reply_to_message
    chat_id = m.chat.id
    thread_id = m.message_thread_id if getattr(m, "topic_message", False) else None
    keyboard = control_panel
    if m.sender_chat:
        return await m.reply_text("you're an __Anonymous__ Admin !\n\n» revert back to user account from admin rights.")
    try:
        aing = await c.get_me()
    except Exception as e:
        return await m.reply_text(f"error:\n\n{e}")
    a = await c.get_chat_member(chat_id, aing.id)
    if a.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await m.reply_text(
            f"💡 To use me, I need to be an **Administrator** with the following **permissions**:\n\n» ❌ __Delete messages__\n» ❌ __Add users__\n» ❌ __Manage video chat__\n\nData is **updated** automatically after you **promote me**"
        )
        return
    priv = a.privileges
    if not (priv and priv.can_manage_video_chats):
        await m.reply_text(
            "missing required permission:" + "\n\n» ❌ __Manage video chat__"
        )
        return
    if not (priv and priv.can_delete_messages):
        await m.reply_text(
            "missing required permission:" + "\n\n» ❌ __Delete messages__"
        )
        return
    if not (priv and priv.can_invite_users):
        await m.reply_text("missing required permission:" + "\n\n» ❌ __Add users__")
        return
    try:
        ubot = (await user.get_me()).id
        b = await get_assistant_member(c, chat_id, ubot)
        if b.status == ChatMemberStatus.BANNED:
            await m.reply_text(
                f"@{ASSISTANT_NAME} **is banned in group** {m.chat.title}\n\n» **unban the userbot first if you want to use this bot.**"
            )
            return
    except (UserNotParticipant, PeerIdInvalid):
        if m.chat.username:
            try:
                await user.join_chat(m.chat.username)
            except UserAlreadyParticipant:
                pass
            except Exception as e:
                await m.reply_text(f"❌ **userbot failed to join**\n\n**reason**: `{e}`")
                return
        else:
            try:
                invitelink = await c.export_chat_invite_link(
                    m.chat.id
                )
                if invitelink.startswith("https://t.me/+"):
                    invitelink = invitelink.replace(
                        "https://t.me/+", "https://t.me/joinchat/"
                    )
                await user.join_chat(invitelink)
            except UserAlreadyParticipant:
                pass
            except Exception as e:
                return await m.reply_text(
                    f"❌ **userbot failed to join**\n\n**reason**: `{e}`"
                )
    # if a previous stream died silently, clear the stale queue so we rejoin
    await drop_stale_queue(chat_id)
    # radio/IPTV never hold a real queue slot — a new /play takes over now
    drop_if_live(chat_id)
    # set after the drop_*() calls above, which may clear_queue() (and with it
    # any stale ACTIVE_THREAD entry) before this session's is recorded
    set_active_thread(chat_id, thread_id)
    if replied:
        # audio sent as a generic file/document (e.g. a bare .mp3) counts too
        audio_doc = (
            replied.document
            if replied.document and (replied.document.mime_type or "").startswith("audio/")
            else None
        )
        if replied.audio or replied.voice or audio_doc:
            media = replied.audio or replied.voice or audio_doc
            ext = os.path.splitext(getattr(media, "file_name", None) or "")[1] or ".m4a"
            cached = os.path.join(os.getcwd(), "downloads", f"{media.file_unique_id}{ext}")
            if os.path.exists(cached) and os.path.getsize(cached) == media.file_size:
                # same Telegram file already fully downloaded — reuse it
                os.utime(cached, None)  # mark fresh for the LRU cache pruner
                suhu = await replied.reply("📦 **already downloaded — starting...**")
                dl = cached
            else:
                suhu = await replied.reply("📥 **downloading audio...**")
                dl = await replied.download(
                    file_name=cached,
                    progress=make_progress(suhu, "📥 Downloading audio"),
                )
            link = replied.link
            # Title preference: audio metadata title > caption > file name > generic
            if replied.audio and replied.audio.title:
                songname = replied.audio.title[:70]
            elif replied.caption:
                songname = str(replied.caption).splitlines()[0][:70]
            elif getattr(media, "file_name", None):
                songname = media.file_name[:70]
            elif replied.voice:
                songname = "Voice Note"
            else:
                songname = "Audio"
            if chat_id in QUEUE:
                pos = add_to_queue(chat_id, songname, dl, link, "Audio", 0)
                if pos == -1:
                    return await suhu.edit(f"🚫 queue is full (max {MAX_QUEUE_SIZE}).")
                await suhu.delete()
                await m.reply_photo(
                    photo=f"{IMG_1}",
                    caption=f"💡 **Track added to queue »** `{pos}`\n\n🏷 **Name:** [{songname}]({link}) | `music`\n💭 **Chat:** `{chat_id}`\n🎧 **Request by:** {m.from_user.mention()}",
                    reply_markup=keyboard,
                )
            else:
             try:
                await suhu.edit("🔄 **Joining vc...**")
                await call_py.play(chat_id, media_audio(dl))
                add_to_queue(chat_id, songname, dl, link, "Audio", 0)
                await suhu.delete()
                requester = f"[{m.from_user.first_name}](tg://user?id={m.from_user.id})"
                await m.reply_photo(
                    photo=f"{IMG_2}",
                    caption=f"🏷 **Name:** [{songname}]({link})\n💭 **Chat:** `{chat_id}`\n💡 **Status:** `Playing`\n🎧 **Request by:** {requester}\n📹 **Stream type:** `Music`",
                    reply_markup=keyboard,
                )
             except Exception as e:
                await suhu.delete()
                await m.reply_text(f"🚫 error:\n\n» {e}")
        else:
            if len(cmd_args) < 2:
                await m.reply(
                    "» reply to an **audio file** or **give something to search.**"
                )
            else:
                suhu = await c.send_message(chat_id, "🔍 **Searching...**", message_thread_id=thread_id)
                query = m.text.split(None, 1)[1]
                found, failure = await find_and_download_music(query, suhu)
                if not found:
                    await suhu.edit(f"❌ **Could not play this music.**\n\n» `{failure[:900]}`")
                else:
                    songname = found["title"]
                    title = songname
                    url = found["url"]
                    duration = found["duration"]
                    thumbnail = found["thumbnail"]
                    ytlink = found["media"]
                    userid = m.from_user.id
                    gcname = m.chat.title
                    try:
                        ctitle = await CHAT_TITLE(gcname)
                        image = await thumb(thumbnail, title, userid, ctitle)
                    except Exception as e:
                        return await suhu.edit(f"🚫 couldn't build the thumbnail: `{e}`")
                    if chat_id in QUEUE:
                        pos = add_to_queue(
                            chat_id, songname, ytlink, url, "Audio", 0
                        )
                        if pos == -1:
                            return await suhu.edit(f"🚫 queue is full (max {MAX_QUEUE_SIZE}).")
                        await suhu.delete()
                        requester = f"[{m.from_user.first_name}](tg://user?id={m.from_user.id})"
                        await m.reply_photo(
                            photo=image,
                            caption=f"💡 **Track added to queue »** `{pos}`\n\n🏷 **Name:** [{songname}]({url}) | `music`\n**⏱ Duration:** `{duration}`\n🎧 **Request by:** {requester}",
                            reply_markup=keyboard,
                        )
                    else:
                        try:
                            await suhu.edit("🔄 **Joining vc...**")
                            await call_py.play(chat_id, media_audio(ytlink))
                            add_to_queue(chat_id, songname, ytlink, url, "Audio", 0)
                            await suhu.delete()
                            requester = f"[{m.from_user.first_name}](tg://user?id={m.from_user.id})"
                            await m.reply_photo(
                                photo=image,
                                caption=f"🏷 **Name:** [{songname}]({url})\n**⏱ Duration:** `{duration}`\n💡 **Status:** `Playing`\n🎧 **Request by:** {requester}\n📹 **Stream type:** `Music`",
                                reply_markup=keyboard,
                            )
                        except Exception as ep:
                            await suhu.delete()
                            await m.reply_text(f"🚫 error: `{ep}`")

    else:
        if len(cmd_args) < 2:
            await m.reply(
                "» reply to an **audio file** or **give something to search.**"
            )
        else:
            suhu = await c.send_message(chat_id, "🔍 **Searching...**", message_thread_id=thread_id)
            query = m.text.split(None, 1)[1]
            found, failure = await find_and_download_music(query, suhu)
            if not found:
                await suhu.edit(f"❌ **Could not play this music.**\n\n» `{failure[:900]}`")
                return

            songname = found["title"]
            url = found["url"]
            duration = found["duration"]
            thumbnail = found["thumbnail"]
            ytlink = found["media"]
            userid = m.from_user.id if m.from_user else 0
            gcname = m.chat.title or "Telegram Chat"
            image = None

            # Artwork is optional and must never prevent audio playback.
            try:
                log.info("Preparing track artwork for provider=%s title=%r", found.get("provider", "unknown"), songname[:80])
                ctitle = await asyncio.wait_for(CHAT_TITLE(gcname), timeout=5)
                image = await asyncio.wait_for(thumb(thumbnail, songname, userid, ctitle), timeout=20)
                log.info("Track artwork prepared successfully")
            except Exception as exc:
                log.warning("Track artwork failed or timed out; continuing without thumbnail: %s: %s", type(exc).__name__, exc)
                image = None

            requester = f"[{m.from_user.first_name}](tg://user?id={m.from_user.id})" if m.from_user else "Unknown"
            if chat_id in QUEUE:
                pos = add_to_queue(chat_id, songname, ytlink, url, "Audio", 0)
                if pos == -1:
                    return await suhu.edit(f"🚫 queue is full (max {MAX_QUEUE_SIZE}).")
                await suhu.delete()
                caption = f"💡 **Track added to queue »** `{pos}`\n\n🏷 **Name:** [{songname}]({url})\n**⏱ Duration:** `{duration}`\n🎧 **Request by:** {requester}"
                if image:
                    await m.reply_photo(photo=image, caption=caption, reply_markup=keyboard)
                else:
                    await m.reply_text(caption, reply_markup=keyboard)
                return

            try:
                await suhu.edit("🔄 **Starting audio stream...**")
                log.info("Calling PyTgCalls play for chat_id=%s provider=%s", chat_id, found.get("provider", "unknown"))
                await asyncio.wait_for(call_py.play(chat_id, media_audio(ytlink)), timeout=45)
                log.info("PyTgCalls play returned successfully for chat_id=%s", chat_id)
                add_to_queue(chat_id, songname, ytlink, url, "Audio", 0)
            except Exception as exc:
                log.exception("PyTgCalls playback failed for chat_id=%s", chat_id)
                try:
                    await suhu.edit(f"🚫 **Could not start playback.**\n\n`{str(exc)[:700]}`", parse_mode=None)
                except Exception:
                    await m.reply_text(f"🚫 Could not start playback: {str(exc)[:700]}", parse_mode=None)
                return

            try:
                await suhu.delete()
            except Exception:
                pass
            caption = _progress_caption(songname, url, duration, requester, 0)
            try:
                if image:
                    status_message = await m.reply_photo(photo=image, caption=caption, reply_markup=keyboard)
                else:
                    status_message = await m.reply_text(caption, reply_markup=keyboard)
                await start_playback_progress(chat_id, status_message, songname, url, duration, requester, keyboard)
            except Exception as exc:
                log.warning("Playback started but status announcement failed: %s", exc)

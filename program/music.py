

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
from youtubesearchpython import VideosSearch


log = logging.getLogger(__name__)

_PROVIDER_FILE = Path("data/music_provider.json")
_PROVIDER_OPTIONS = {
    "auto": "Auto (fallback)",
    "youtube": "YouTube",
    "youtube_music": "YouTube Music",
    "soundcloud": "SoundCloud",
    "google_videos": "Google Videos",
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

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _ytdl_site_flags():
    """Build yt-dlp flags shared by supported sites."""
    flags = ["--socket-timeout", "60", "--retries", "5", "--fragment-retries", "5", "--extractor-retries", "3", "--file-access-retries", "3"]
    if COOKIES_FILE:
        flags += ["--cookies", COOKIES_FILE]
    # Do not force a desktop Chrome UA on YouTube's TV/mobile player clients:
    # mismatched client headers can cause media URLs to be rejected with HTTP 403.
    if PROXY_URL:
        flags += ["--proxy", PROXY_URL]
    return flags


def _youtube_client_args():
    """Let current yt-dlp choose supported YouTube player clients automatically.

    Hard-coding legacy clients (such as ``tv``) can trigger YouTube's
    "The page needs to be reloaded" response or hide all downloadable formats.
    """
    return []

def ytsearch(query: str):
    try:
        q = query.strip()
        is_yt = bool(re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", q))
        if re.match(r"https?://", q):
            # any direct URL — use yt-dlp for metadata (handles YouTube, Rutube, Vimeo, …)
            cmd = ["yt-dlp", "--no-warnings", "--skip-download",
                   "--print", "%(title)s\x1f%(duration_string)s\x1f%(id)s\x1f%(thumbnail)s"]
            cmd += _ytdl_site_flags()
            if is_yt:
                cmd += _youtube_client_args()
            cmd.append(q)
            out = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=90,
            ).stdout.strip().split("\x1f")
            title = out[0] if out and out[0] else q
            duration = out[1] if len(out) > 1 else ""
            vid = out[2] if len(out) > 2 and out[2] else ""
            raw_thumb = out[3] if len(out) > 3 else ""
            # prefer the well-known HQ thumbnail URL for YouTube
            thumbnail = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if is_yt and vid else raw_thumb
            return [title, q, duration, thumbnail]
        search = VideosSearch(query, limit=1).result()
        data = search["result"][0]
        songname = data["title"]
        url = data["link"]
        duration = data["duration"]
        thumbnail = f"https://i.ytimg.com/vi/{data['id']}/hqdefault.jpg"
        return [songname, url, duration, thumbnail]
    except Exception as e:
        log.warning("ytsearch failed: %s", e)
        return 0


def ytsearch_all(query: str, limit: int = 5):
    """Search multiple catalogs in selected-provider-first order, then fall back."""
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
        first = ytsearch(q)
        if first:
            add(*first, provider="Direct URL")
        return candidates

    selected = _get_provider()
    if selected in ("auto", "youtube", "youtube_music"):
        try:
            result = VideosSearch(q, limit=limit).result().get("result", [])
            for item in result:
                vid = item.get("id", "")
                add(item.get("title"), item.get("link"), item.get("duration"),
                    f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "",
                    "YouTube Music" if selected == "youtube_music" else "YouTube")
        except Exception as exc:
            log.warning("YouTube search provider failed: %s", exc)

    search_sources = [
        ("scsearch", "SoundCloud", "soundcloud"),
        ("ytsearch", "YouTube (yt-dlp)", "youtube"),
        ("gvsearch", "Google Videos", "google_videos"),
    ]
    if selected != "auto":
        search_sources.sort(key=lambda item: 0 if item[2] == selected else 1)
    for prefix, provider, key in search_sources:
        if key == "youtube" and selected in ("auto", "youtube", "youtube_music"):
            continue
        try:
            proc = subprocess.run(
                ["yt-dlp", "--no-warnings", "--skip-download", "--flat-playlist",
                 "--dump-single-json", *_ytdl_site_flags(), f"{prefix}{limit}:{q}"],
                capture_output=True, text=True, timeout=22,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                log.warning("%s search failed: %s", provider, (proc.stderr or "")[-250:])
                continue
            data = json.loads(proc.stdout)
            entries = data.get("entries") or [data]
            for item in entries:
                url = item.get("webpage_url") or item.get("url")
                if url and not str(url).startswith(("http://", "https://")):
                    if "youtube" in provider.lower() and item.get("id"):
                        url = "https://www.youtube.com/watch?v=" + item["id"]
                    else:
                        continue
                add(item.get("title"), url, item.get("duration_string") or item.get("duration") or "",
                    item.get("thumbnail") or "", provider)
        except subprocess.TimeoutExpired:
            log.warning("%s search timed out; moving to next provider", provider)
        except Exception as exc:
            log.warning("%s search provider error: %s", provider, exc)
    return candidates


_YT_RE = re.compile(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/")
_SOUNDCLOUD_RE = re.compile(r"https?://(www\.)?soundcloud\.com/", re.I)


async def _download_soundcloud(link: str):
    """Download SoundCloud audio locally instead of feeding a short-lived URL to FFmpeg.

    Some SoundCloud stream URLs resolve successfully in yt-dlp but then produce an
    immediate EOF in the NTgCalls FFmpeg reader. A local file avoids that direct-URL
    playback path and makes extraction/download failures visible in the logs.
    """
    started_at = time()
    downloads = Path("downloads")
    downloads.mkdir(parents=True, exist_ok=True)
    log.info("Downloading SoundCloud track locally before playback: %s", link[:180])
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--no-warnings", "--no-playlist", "--no-part", "--newline",
            *_ytdl_site_flags(),
            "-f", "bestaudio/best",
            "-o", str(downloads / "%(extractor)s-%(id)s.%(ext)s"),
            "--print", "after_move:filepath",
            link,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:
        log.exception("Could not launch yt-dlp for SoundCloud download")
        return 0, "Could not start yt-dlp for SoundCloud download."
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        log.warning("SoundCloud download timed out after 120s")
        proc.kill()
        await proc.wait()
        return 0, "SoundCloud audio download timed out after 120 seconds."
    elapsed = time() - started_at
    lines = [line.strip() for line in stdout.decode(errors="ignore").splitlines() if line.strip()]
    local_path = Path(lines[-1]) if lines else None
    if proc.returncode == 0 and local_path and local_path.is_file() and local_path.stat().st_size > 16_000:
        log.info("SoundCloud audio downloaded locally in %.1fs (%s bytes): %s", elapsed, local_path.stat().st_size, local_path)
        return 1, str(local_path.resolve())
    err = stderr.decode(errors="ignore")[-700:]
    log.warning("SoundCloud local download failed (exit=%s, %.1fs): %s", proc.returncode, elapsed, err or "no output file")
    return 0, (err or "SoundCloud download did not produce a valid audio file.")


async def ytdl(format: str, link: str, status_msg=None):
    if _SOUNDCLOUD_RE.match(link):
        return await _download_soundcloud(link)
    # For non-YouTube sites (e.g. Rutube) the formats are HLS-only muxed streams
    # that would require downloading gigabytes before playback.  Instead, extract
    # the direct stream URL and hand it to ffmpeg live (same as radio).
    # YouTube must still be downloaded first because its URLs 403 ffmpeg directly.
    if not _YT_RE.match(link):
        started_at = time()
        log.info("Starting yt-dlp stream resolution for non-YouTube URL: %s", link[:180])
        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "--no-warnings", "--no-playlist",
                *_ytdl_site_flags(),
                "-f", "worstaudio/worst",
                "--print", "%(url)s",
                link,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            log.exception("Could not launch yt-dlp for URL %s", link[:180])
            return 0, "Could not start yt-dlp; check the Railway runtime and executable path."
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)
        except asyncio.TimeoutError:
            log.warning("yt-dlp stream resolution timed out after 35s: %s", link[:180])
            proc.kill()
            await proc.wait()
            return 0, "Provider timed out while resolving the audio stream (35 seconds)."
        elapsed = time() - started_at
        if proc.returncode == 0:
            # --print may emit more than one line; the final non-empty line is the URL.
            lines = [line.strip() for line in stdout.decode(errors="ignore").splitlines() if line.strip()]
            url = lines[-1] if lines else ""
            if url.startswith(("https://", "http://")):
                log.info("yt-dlp resolved audio stream in %.1fs", elapsed)
                return 1, url
        err = stderr.decode(errors="ignore")[-400:]
        log.warning("yt-dlp stream resolution failed (exit=%s, %.1fs): %s", proc.returncode, elapsed, err or "no stderr")
        return 0, (err or "stream URL extraction failed")

    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        *_ytdl_site_flags(),
        *(["--sponsorblock-remove", SPONSORBLOCK_REMOVE] if SPONSORBLOCK_REMOVE else []),
        "--no-simulate",
        "--newline",
        "--progress-template",
        "download:PROG|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        # Use yt-dlp's current default YouTube clients; hard-coded legacy clients
        # can trigger reload/SABR errors and hide audio formats.
        *(_youtube_client_args() if _YT_RE.match(link) else []),
        "--print",
        "after_move:filepath",
        "-f",
        "bestaudio/best",
        "-o",
        "downloads/%(id)s.%(ext)s",
        f"{link}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_buf = []

    async def _drain_stderr():
        while True:
            chunk = await proc.stderr.readline()
            if not chunk:
                break
            stderr_buf.append(chunk.decode(errors="ignore"))

    stderr_task = asyncio.ensure_future(_drain_stderr())

    path = ""
    last_edit = 0.0
    while True:
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=75)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await stderr_task
            return 0, "YouTube download timed out after 75 seconds; trying another provider."
        if not raw:
            break
        line = raw.decode(errors="ignore").strip()
        if not line:
            continue
        if line.startswith("PROG|"):
            if status_msg is not None and time() - last_edit >= 3:
                last_edit = time()
                parts = line.split("|")
                pct = parts[1].strip() if len(parts) > 1 else ""
                spd = parts[2].strip() if len(parts) > 2 else ""
                eta = parts[3].strip() if len(parts) > 3 else ""
                try:
                    await status_msg.edit(
                        f"📥 **Downloading from YouTube…** `{pct}`\n({spd}, ETA {eta})"
                    )
                except Exception:
                    pass
        else:
            path = line  # --print after_move:filepath is the last line on success
    await proc.wait()
    await stderr_task
    if proc.returncode == 0 and path:
        return 1, path
    return 0, ("".join(stderr_buf)[-500:] or "download failed")


async def find_and_download_music(query: str, status_msg=None):
    """Search multiple catalogs and try candidates until one streams/downloads."""
    # Search providers in a worker thread so a slow external search cannot block Telegram updates.
    log.info("Starting music search for query=%r", query[:120])
    candidates = await asyncio.to_thread(ytsearch_all, query, 5)
    log.info("Music search returned %d candidate(s)", len(candidates))
    if not candidates:
        return None, "No results found across YouTube and SoundCloud."
    errors = []
    for index, candidate in enumerate(candidates, start=1):
        title, url, duration, thumbnail, provider = candidate
        started_at = time()
        log.info("Candidate %d/%d: provider=%s title=%r url=%s", index, len(candidates), provider, title[:100], url[:180])
        try:
            if status_msg is not None:
                await status_msg.edit(f"🔎 **Trying {provider or 'music provider'}** ({index}/{len(candidates)})…\n\n🏷 `{title[:70]}`")
            # A single extractor must never leave the request stuck indefinitely.
            attempt_timeout = 145 if _SOUNDCLOUD_RE.match(url) else 85
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
        "Choose a preferred source. If it fails, the bot will try other sources.",
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
            "Other supported sources remain available as fallback.",
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
            caption = f"🏷 **Name:** [{songname}]({url})\n**⏱ Duration:** `{duration}`\n💡 **Status:** `Playing`\n🎧 **Request by:** {requester}\n📹 **Stream type:** `Music`"
            try:
                if image:
                    await m.reply_photo(photo=image, caption=caption, reply_markup=keyboard)
                else:
                    await m.reply_text(caption, reply_markup=keyboard)
            except Exception as exc:
                log.warning("Playback started but status announcement failed: %s", exc)

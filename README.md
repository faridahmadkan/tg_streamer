# tg-streaming-bot — restricted groups, no-Docker web deployment

A Telegram music/video streaming bot that preserves the original Python + Pyrogram/Kurigram + PyTgCalls engine. A small **Node.js web service** supervises the Python process and exposes `/health` for Railway or another web-service host. **Docker is not required.**

## Features retained

- Music and video streaming in Telegram group voice chats from YouTube searches/URLs, Telegram media, and live links.
- YouTube search picker, radio stations, IPTV catalogue, local media library, recording, screenshots, inline now-playing controls, queue controls, pause/resume/skip/seek, volume/mute, loop/shuffle/clear, autoplay, resume after restart, idle auto-leave, custom extractors, optional browser fallback, and the original plugins.
- Per-user rate limiting and maximum queue size.

## Access rules

1. `BOT_ADMINS` is the comma-separated list of Telegram **user IDs** allowed to use private bot commands, including `/start`, and owner-only commands.
2. `ALLOWED_GROUPS` is the comma-separated list of approved Telegram **group/supergroup chat IDs**. Supergroup IDs normally look like `-1001234567890`.
3. In an approved group, only Telegram group administrators can use bot commands or inline callback controls. The check fails closed if Telegram cannot verify the administrator list.
4. If the bot is added to a group not in `ALLOWED_GROUPS`, it automatically leaves that group.
5. The assistant user account must also be a member of each approved group where you want to stream. The bot account itself needs sufficient group permissions for its features.

## Configure environment variables

Copy `.env.example` to `.env` locally, or enter these values as environment variables in your hosting dashboard. Never commit real tokens or session strings.

```dotenv
API_ID=your_numeric_api_id
API_HASH=your_api_hash
BOT_TOKEN=123456:your_bot_token
BOT_USERNAME=your_bot_username_without_at
BOT_NAME=Music Bot
SESSION_NAME=your_assistant_pyrogram_session_string
BOT_ADMINS=123456789,987654321
ALLOWED_GROUPS=-1001234567890,-1009876543210
```

Required values:

| Variable | Purpose |
|---|---|
| `API_ID`, `API_HASH` | Telegram API credentials from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `BOT_USERNAME` | Bot username without `@` |
| `SESSION_NAME` | Pyrogram session string for the assistant user account |
| `BOT_ADMINS` | Comma-separated numeric user IDs allowed in private chat / owner-only commands |
| `ALLOWED_GROUPS` | Comma-separated numeric group IDs allowed to use the bot |

Other original settings remain supported; see `example.env` for options such as `DURATION_LIMIT`, `RADIO_STATIONS`, `LIBRARY_ROOT`, `MAX_QUEUE_SIZE`, `RATE_LIMIT_MAX`, `PROXY_*`, and media-card images.

## Generate the assistant session (on your own computer)

Use a dedicated Telegram user account for the assistant. From the project directory, run:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\\Scripts\\python -m pip install -r requirements.txt
.venv\\Scripts\\python gen_session.py
```

On Linux/macOS use `.venv/bin/python -m pip install -r requirements.txt` and `.venv/bin/python gen_session.py`. Copy the generated session string into `SESSION_NAME`. Do not share it; it grants access to that Telegram account.

## Deploy to Railway without Docker

1. Push this repository to your own GitHub repository.
2. Create a Railway project and deploy the repository as a **Web Service**. Do not select a Dockerfile deployment.
3. Railway uses `nixpacks.toml` to install Node.js dependencies, Python 3.11, FFmpeg, and the original Python requirements. The start command is `node server.js`.
4. Add the required environment variables above in Railway's **Variables** page. You may paste the contents of `.env` there, but do not commit secrets to GitHub.
5. Set the health-check path to `/health` if the service settings offer a health-check field.
6. Keep the service running continuously. Voice-chat streaming requires a continuously running worker; a sleeping/short-lived web host will interrupt playback.
7. Add the bot to each configured group, promote it with the permissions the original README describes, and make sure the assistant account has joined that group. Start a group voice chat and try `/play <song>` as a group administrator.

The HTTP service reports `200` at `/health` while the Python bot process is alive and `503` if the bot is stopped or has exited. The Node process exits when the Python process exits so the host can detect and restart a crashed bot.

## Local development (no Docker)

Install Python 3.11, Node.js 20+, and FFmpeg. Then:

```bash
python -m venv .venv
# Install Python requirements using the venv's pip (see session instructions above).
npm start
```

Open `http://localhost:3000/health` (or the port in `PORT`) to check the service.

## Important notes

- Telegram voice streaming depends on PyTgCalls/NTgCalls and the assistant user session; the Node.js layer is only the web-service supervisor and health endpoint. It does not replace or reimplement the streaming engine.
- All group commands and button callbacks are restricted centrally. If an administrator was just promoted/demoted and access seems stale, run `/reload` as a group admin to refresh the cached list.
- Use only media you have permission to stream or download, and comply with Telegram and source-service terms.


### YouTube HTTP 403 troubleshooting

The bot uses a dedicated YouTube player client for audio/video extraction and
does not force a desktop browser User-Agent onto that client. yt-dlp is installed
from `requirements.txt`; redeploy after updating dependencies. YouTube can still
block datacenter IPs or require account cookies for some videos. If 403 persists,
set `COOKIES_BASE64` to a Base64-encoded Netscape-format `cookies.txt` exported
from your own signed-in browser session (keep it secret; never commit it), or try
a different hosting region/network. Cookies cannot guarantee access to private,
age-restricted, or otherwise unavailable videos.

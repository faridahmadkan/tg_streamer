import os

from config import API_HASH, API_ID, BOT_TOKEN, SESSION_NAME
from pyrogram import Client
from pytgcalls import PyTgCalls

# Resolve the project root from this file so the path works regardless of
# the directory from which Railway starts the process.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")

# Pyrogram stores the bot account's SQLite session database in workdir.
# Railway's checkout does not include the ignored downloads/ directory, so
# create it before Client is initialized or started.
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Optional proxy support (for networks where Telegram is filtered).
# Set PROXY_HOST/PROXY_PORT in the environment to enable it.
PROXY = None
if os.getenv("PROXY_HOST"):
    PROXY = {
        "scheme": os.getenv("PROXY_SCHEME", "socks5"),
        "hostname": os.getenv("PROXY_HOST"),
        "port": int(os.getenv("PROXY_PORT", "1080")),
    }
    if os.getenv("PROXY_USER"):
        PROXY["username"] = os.getenv("PROXY_USER")
        PROXY["password"] = os.getenv("PROXY_PASS", "")

# Telegram bot account. Its SQLite session is stored under downloads/.
bot = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins={"root": "program"},
    proxy=PROXY,
    workdir=DOWNLOADS_DIR,
)

# Assistant user account authenticates using the existing session string.
user = Client(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_NAME,
    proxy=PROXY,
)

call_py = PyTgCalls(user)

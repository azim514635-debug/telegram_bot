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
  /links      - View recent uploaded links
  /delete     - Delete a link by ID
  /broadcast  - Send a notification to all users
  /stats      - Bot usage statistics
  /cancel     - Cancel current operation
"""

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

# ── Constants ───────────────────────────────────────────────────────
BASE_URL = "https://azim-studio.onrender.com"
ENDPOINT = "/api/upload/link-item"
CONFIG_FILE = Path(__file__).parent / "bot_config.json"
STATS_FILE = Path(__file__).parent / "bot_stats.json"

# Conversation states
WAIT_TITLE, WAIT_URL, WAIT_THUMBNAIL, WAIT_CONFIRM = range(4)
WAIT_SECRET, WAIT_BROADCAST_MSG, WAIT_DELETE_ID = range(4, 7)

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


def fetch_links():
    status, body = api_request("/api/links")
    if status == 200 and isinstance(body, list):
        return body
    return []


def delete_link_api(link_id):
    status, body = api_request(
        f"/api/media/link/{link_id}",
        "DELETE",
        {"bossSecret": config.boss_secret},
    )
    return status == 200 and isinstance(body, dict) and body.get("success")


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
    await update.get_chat().send_action("typing")


def mask_secret(s):
    if len(s) > 6:
        return s[:3] + "***" + s[-3:]
    return "***"


def escape_html(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── /start ─────────────────────────────────────────────────────────
@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(update)
    name = update.effective_user.first_name or "there"
    text = (
        f"Welcome, <b>{escape_html(name)}</b>!\n\n"
        "I'm the <b>Azim's Space</b> upload bot.\n"
        "Use the buttons below or send /commands.\n\n"
        "<i>Tip: Use /upload to quickly add a link.</i>\n\n"
        "First time? Send /myid to get your ID, then /setowner to lock the bot."
    )
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=main_menu_keyboard()
    )


# ── /help ──────────────────────────────────────────────────────────
@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(update)
    text = (
        "<b>Commands</b>\n\n"
        "/myid — Get your Telegram user ID\n"
        "/setowner — Lock bot to only your ID\n"
        "/upload — Upload a new link\n"
        "/links — View recent links\n"
        "/delete — Delete a link by ID\n"
        "/broadcast — Send notification to all users\n"
        "/stats — View usage statistics\n"
        "/setsecret — Update the boss secret\n"
        "/cancel — Cancel current operation\n"
        "/help — Show this message\n"
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


# ── Menu callback router ───────────────────────────────────────────
@admin_only_cb
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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
        links = fetch_links()
        if not links:
            await query.edit_message_text(
                "No links found.", reply_markup=back_button()
            )
            return
        text = "<b>Recent Links</b>\n\n"
        for i, link in enumerate(links[:10], 1):
            title = escape_html(link.get("title", "Untitled"))
            lid = link.get("id", "?")
            url = link.get("url", "")
            text += f"<b>{i}.</b> {title}\n"
            text += f"   <code>{lid}</code>\n"
            if url:
                text += f'   <a href="{escape_html(url)}">Open</a>\n'
            text += "\n"
        text += "Send /delete to remove a link."
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
            "/links — View recent links\n"
            "/delete — Delete a link by ID\n"
            "/broadcast — Send notification to all users\n"
            "/stats — View usage statistics\n"
            "/setsecret — Update the boss secret\n"
            "/cancel — Cancel current operation\n"
            "/help — Show this message\n"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button())


# ── Upload conversation (with inline confirmation) ─────────────────
@admin_only
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
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(
            "Access denied.\n"
            f"Your ID: <code>{update.effective_user.id}</code>",
            parse_mode="HTML",
        )
        return

    state = context.user_data.get("state")
    text = update.message.text.strip()

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

    # No active state — show hint
    await update.message.reply_text(
        "Send /upload to start, or /help for commands."
    )


# ── Upload confirmation callback ───────────────────────────────────
@admin_only_cb
async def upload_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = context.user_data.get("upload", {})

    if not data or not data.get("url"):
        await query.edit_message_text("Upload data expired. Start again with /upload.")
        return

    await query.edit_message_text("Uploading...")
    success, result = upload_link_api(
        data["title"], data["url"], data.get("thumbnail", "")
    )

    if success:
        stats.inc("uploads")
        item_id = result.get("id", "?") if isinstance(result, dict) else result
        text = (
            "<b>Upload Successful</b>\n\n"
            f"Title: <b>{escape_html(data['title'])}</b>\n"
            f"ID: <code>{escape_html(str(item_id))}</code>\n"
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


@admin_only_cb
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
        BotCommand("myid", "Get your Telegram user ID"),
        BotCommand("setowner", "Lock bot to only your ID"),
        BotCommand("upload", "Upload a new link"),
        BotCommand("links", "View recent links"),
        BotCommand("delete", "Delete a link by ID"),
        BotCommand("broadcast", "Send notification to all users"),
        BotCommand("stats", "View usage statistics"),
        BotCommand("setsecret", "Update the boss secret"),
        BotCommand("help", "Show help"),
        BotCommand("cancel", "Cancel current operation"),
    ])
    logger.info("Bot commands registered.")


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
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("setowner", cmd_setowner))
    app.add_handler(CommandHandler("links", cmd_start))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(menu_router, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(upload_confirm_cb, pattern="^upload_confirm$"))
    app.add_handler(CallbackQueryHandler(upload_cancel_cb, pattern="^upload_cancel$"))
    app.add_handler(CallbackQueryHandler(broadcast_send_cb, pattern="^broadcast_send$"))
    app.add_handler(CallbackQueryHandler(broadcast_cancel_cb, pattern="^broadcast_cancel$"))

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

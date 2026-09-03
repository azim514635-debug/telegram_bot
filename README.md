# Azim's Space — Telegram Bot

Professional Telegram bot for uploading links to [Azim's Space](https://azim-studio.onrender.com).

## Deploy on Render

1. Push this repo to GitHub
2. Go to https://dashboard.render.com → **New** → **Blueprint**
3. Connect this repo and click **Apply**
4. Set environment variables:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `WEBHOOK_SECRET` — any random string
5. Bot starts automatically in webhook mode

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env
# Add your bot token to .env
python telegram_bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/upload` | Upload a link |
| `/links` | View recent links |
| `/delete` | Delete a link |
| `/broadcast` | Send notification |
| `/stats` | View statistics |
| `/setsecret` | Set boss secret |
| `/cancel` | Cancel operation |

## Secretary Mode (Instant Get File)

When a visitor clicks **"Instant Get File"** on the website, the bot forwards the
same file to a third-party link-generator bot, parses the download URL from its
reply, and sends it to the website user.

### Setup (important)

1. Set `LINK_GENERATOR_BOT` to the @username (no `@`) of your link generator bot.
2. **Introduce the two bots** — your bot can only send files to the link
   generator bot once they share a chat. Do this once from your Telegram:
   - Open the link generator bot (@File_To_Link_2Bot) → press **Start** → send a
     test file so it replies.
   - Forward a message from your own bot into the link generator bot's chat (or
     forward the link generator bot's reply into your bot's chat).
3. Also set on Render: `BIN_CHANNEL_ID` (numeric channel id) and `BOT_USERNAME`
   for the deep-link "Get file in Telegram" button.

### Boss-only uploads

Only the BOSS (set by `OWNER_CHAT_ID` or `/setowner`) is allowed to publish
forwarded files to the website. Any other user forwarding a file only receives
the Telegram deep-link — their files are never uploaded to your website.

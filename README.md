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

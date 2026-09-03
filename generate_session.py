#!/usr/bin/env python3
"""Generate a Telethon StringSession for the Secretary Mode user client.

Run this ONCE on your own machine/phone (where you have Telegram logged in) to
produce a session string, then paste it into the USER_STRING_SESSION env var on
Render. It lets the user client act as YOUR human account so it can send files
to @File_To_Link_2Bot (bots cannot message bots).

Usage:
    pip install telethon
    python generate_session.py

You will be asked for your phone number and the login code Telegram sends.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = input("Enter your API ID: ").strip()
API_HASH = input("Enter your API Hash: ").strip()

client = TelegramClient(StringSession(), int(API_ID), API_HASH)
client.start()
session_str = client.session.save()
print("\n" + "=" * 60)
print("YOUR USER STRING SESSION (keep it secret):")
print(session_str)
print("=" * 60)
print("\nPaste the above into the USER_STRING_SESSION env var on Render.")
client.disconnect()

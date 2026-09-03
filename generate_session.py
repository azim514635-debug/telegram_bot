#!/usr/bin/env python3
"""Generate your Telethon StringSession for Secretary Mode — run this yourself.

Run in a normal terminal (input prompts work there):
    pip install telethon
    python generate_my_session.py

You'll be asked for: API ID, API Hash, phone number, login code (+ 2FA if any).
At the end it prints your USER_STRING_SESSION — paste that into the
USER_STRING_SESSION env var on Render.
"""
import asyncio, logging

logging.basicConfig(level=logging.INFO)
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = input("API ID: ").strip()
    api_hash = input("API Hash: ").strip()
    if not (api_id and api_hash):
        print("API ID and API Hash are required.")
        return

    client = TelegramClient(
        StringSession(), int(api_id), api_hash, device_model="Azim Secretary"
    )
    await client.connect()
    if await client.is_user_authorized():
        print("Already authorized on this session.")
        s = client.session.save()
    else:
        phone = input("Phone (intl format, e.g. +1234567890): ").strip()
        await client.send_code_request(phone)
        code = input("Login code from Telegram: ").strip()
        try:
            await client.sign_in(phone, code)
        except Exception:
            pwd = input("2FA password (if any): ").strip()
            await client.sign_in(password=pwd)
        s = client.session.save()

    print("\n" + "=" * 60)
    print("YOUR USER STRING SESSION (keep it secret):")
    print(s)
    print("=" * 60)
    me = await client.get_me()
    print("\nLogged in as:", me.first_name, "@" + (me.username or ""))
    await client.disconnect()
    print("\nPaste the STRING SESSION above into USER_STRING_SESSION on Render.")


if __name__ == "__main__":
    asyncio.run(main())

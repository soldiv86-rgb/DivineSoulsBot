"""
DivineSoul entrypoint.

Render start command:
  python bot.py

Env required:
  DISCORD_TOKEN, DASHBOARD_CHANNEL_ID, REPORT_SECRET, DASHBOARD_KEY
Optional:
  PORT, DATA_FILE, THEME_FILE
"""
import asyncio
import logging

import discord

from divinesoul.config import DISCORD_TOKEN
from divinesoul.persistence import load_accounts, load_theme
from divinesoul.discord_bot import bot
from divinesoul.web_app import start_web_server


async def main():
    discord.utils.setup_logging(level=logging.INFO)
    load_accounts()
    load_theme()
    asyncio.create_task(start_web_server())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

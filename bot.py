import asyncio
import time
from aiohttp import web
import discord
from discord.ext import tasks, commands

# ---- CONFIG ----
import os

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DASHBOARD_CHANNEL_ID = int(os.environ["DASHBOARD_CHANNEL_ID"])
REPORT_SECRET = os.environ["REPORT_SECRET"]
OFFLINE_TIMEOUT_MULTIPLIER = 2
WEB_SERVER_PORT = int(os.environ.get("PORT", 8080))

# ---- STATE ----
accounts = {}  # label -> {placeId, jobId, gameName, lastSeen, intervalSeconds}
dashboard_message_id = None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def build_dashboard_embed():
    embed = discord.Embed(title="Account Dashboard", color=0xFF8C28)
    if not accounts:
        embed.description = "No accounts reporting yet."
        return embed, []

    view = discord.ui.View(timeout=None)
    now = time.time()

    for label, data in sorted(accounts.items()):
        elapsed = now - data["lastSeen"]
        is_online = elapsed <= data["intervalSeconds"] * OFFLINE_TIMEOUT_MULTIPLIER
        status = "🟢 Online" if is_online else "🔴 Offline"

        embed.add_field(
            name=f"{label} - {status}",
            value=f"Game: {data['gameName']}\nLast seen: {int(elapsed)}s ago",
            inline=False,
        )

        if is_online:
            join_url = (
                f"https://www.roblox.com/games/start?"
                f"placeId={data['placeId']}&gameInstanceId={data['jobId']}"
            )
            view.add_item(discord.ui.Button(label=f"Join {label}", url=join_url))

    return embed, view


@tasks.loop(seconds=15)
async def refresh_dashboard():
    global dashboard_message_id
    channel = bot.get_channel(DASHBOARD_CHANNEL_ID)
    if not channel:
        return

    embed, view = build_dashboard_embed()

    if dashboard_message_id is None:
        msg = await channel.send(embed=embed, view=view)
        dashboard_message_id = msg.id
    else:
        try:
            msg = await channel.fetch_message(dashboard_message_id)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            msg = await channel.send(embed=embed, view=view)
            dashboard_message_id = msg.id


# ---- HTTP endpoint the Roblox scripts report to ----
async def handle_report(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if data.get("secret") != REPORT_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)

    label = data.get("label")
    if not label:
        return web.json_response({"error": "missing label"}, status=400)

    accounts[label] = {
        "placeId": data.get("placeId"),
        "jobId": data.get("jobId"),
        "gameName": data.get("gameName", "Unknown"),
        "lastSeen": time.time(),
        "intervalSeconds": data.get("intervalSeconds", 300),
    }
    return web.json_response({"ok": True})


async def start_web_server():
    app = web.Application()
    app.router.add_post("/report", handle_report)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_SERVER_PORT)
    await site.start()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not refresh_dashboard.is_running():
        refresh_dashboard.start()


@bot.command()
async def dashboard(ctx):
    """Manually post a fresh dashboard message."""
    global dashboard_message_id
    embed, view = build_dashboard_embed()
    msg = await ctx.send(embed=embed, view=view)
    dashboard_message_id = msg.id


async def main():
    asyncio.create_task(start_web_server())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

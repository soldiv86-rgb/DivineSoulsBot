import asyncio
import json
import os
import time

from aiohttp import web
import discord
from discord.ext import tasks, commands

# ---- CONFIG ----
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DASHBOARD_CHANNEL_ID = int(os.environ["DASHBOARD_CHANNEL_ID"])
REPORT_SECRET = os.environ["REPORT_SECRET"]
OFFLINE_TIMEOUT_MULTIPLIER = 2
WEB_SERVER_PORT = int(os.environ.get("PORT", 8080))
STATE_FILE = "dashboard_state.json"  # just the message id, so a restart doesn't spawn a duplicate dashboard message

# ---- STATE ----
accounts = {}  # label -> {placeId, jobId, gameName, lastSeen, intervalSeconds}
dashboard_message_id = None


def load_dashboard_message_id():
    global dashboard_message_id
    try:
        with open(STATE_FILE, "r") as f:
            dashboard_message_id = json.load(f).get("dashboard_message_id")
    except (FileNotFoundError, json.JSONDecodeError):
        dashboard_message_id = None


def save_dashboard_message_id():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"dashboard_message_id": dashboard_message_id}, f)
    except OSError:
        pass


intents = discord.Intents.default()
# Required for prefix commands (!dashboard, !remove) to receive message text
# at all in discord.py 2.x. You ALSO need to flip "Message Content Intent"
# on for this bot in the Discord Developer Portal (Bot tab) - the code-side
# flag alone isn't enough, it's a privileged intent.
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def format_elapsed(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def build_dashboard_embed():
    embed = discord.Embed(title="Account Dashboard", color=0xFF8C28)
    if not accounts:
        embed.description = "No accounts reporting yet."
        return embed, None

    view = discord.ui.View(timeout=None)
    now = time.time()

    for label, data in sorted(accounts.items()):
        elapsed = now - data["lastSeen"]
        is_online = elapsed <= data["intervalSeconds"] * OFFLINE_TIMEOUT_MULTIPLIER
        status = "🟢 Online" if is_online else "🔴 Offline"

        embed.add_field(
            name=f"{label} - {status}",
            value=f"Game: {data['gameName']}\nLast seen: {format_elapsed(elapsed)} ago",
            inline=False,
        )

        # Only show a Join button when we actually have both ids - a
        # missing placeId/jobId would otherwise build a broken link
        # (e.g. "...gameInstanceId=None") instead of just omitting the button.
        if is_online and data.get("placeId") and data.get("jobId"):
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

    try:
        if dashboard_message_id is None:
            msg = await channel.send(embed=embed, view=view)
            dashboard_message_id = msg.id
            save_dashboard_message_id()
        else:
            try:
                msg = await channel.fetch_message(dashboard_message_id)
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                msg = await channel.send(embed=embed, view=view)
                dashboard_message_id = msg.id
                save_dashboard_message_id()
    except discord.HTTPException as e:
        # A transient Discord API hiccup (rate limit, momentary outage)
        # shouldn't permanently kill the whole refresh loop - previously an
        # uncaught exception here would stop tasks.loop for good until the
        # bot process was restarted. Now it just skips this tick.
        print(f"[dashboard] refresh failed, will retry next tick: {e}")


# ---- HTTP endpoints ----
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


async def handle_health(request):
    # Render (and most hosts) periodically GET "/" to check the service is
    # alive - without this route it 404s, and the host can decide the
    # service is unhealthy and cycle it.
    return web.json_response({"ok": True, "accounts": len(accounts)})


async def start_web_server():
    app = web.Application()
    app.router.add_post("/report", handle_report)
    app.router.add_get("/", handle_health)
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
    save_dashboard_message_id()


@bot.command()
async def remove(ctx, label: str):
    """Remove an account from the dashboard permanently (e.g. retired for good)."""
    if label in accounts:
        del accounts[label]
        await ctx.send(f"Removed `{label}` from the dashboard.")
    else:
        await ctx.send(f"No account labeled `{label}` found.")

@bot.command() async def ping(ctx): await ctx.send("pong") Redeploy, then type !ping

async def main():
    load_dashboard_message_id()
    asyncio.create_task(start_web_server())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

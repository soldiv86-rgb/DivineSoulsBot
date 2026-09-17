import asyncio
import logging
import os
import time

from aiohttp import web
import discord
from discord.ext import commands

# ---- CONFIG ----
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DASHBOARD_CHANNEL_ID = int(os.environ["DASHBOARD_CHANNEL_ID"])
REPORT_SECRET = os.environ["REPORT_SECRET"]
OFFLINE_TIMEOUT_MULTIPLIER = 2
WEB_SERVER_PORT = int(os.environ.get("PORT", 8080))
MAX_JOIN_BUTTONS = 25  # Discord's hard cap on components per view (5 rows x 5)

# ---- STATE ----
accounts = {}  # label -> {placeId, jobId, gameName, lastSeen, intervalSeconds}

intents = discord.Intents.default()
intents.message_content = True  # needed for the !panel/!dashboard/!userlist/!remove text commands
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


def is_account_online(data, now):
    return (now - data["lastSeen"]) <= data["intervalSeconds"] * OFFLINE_TIMEOUT_MULTIPLIER


def build_join_view(entries_with_join_info):
    """entries_with_join_info: list of (label, placeId, jobId). Returns a
    View of link buttons, or None if there's nothing to add - link buttons
    don't need custom_id/persistence since Discord opens the URL directly
    without ever calling back into the bot."""
    view = discord.ui.View(timeout=None)
    for label, place_id, job_id in entries_with_join_info:
        if len(view.children) >= MAX_JOIN_BUTTONS:
            break
        if place_id and job_id:
            join_url = f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"
            view.add_item(discord.ui.Button(label=f"Join {label}", url=join_url))
    return view if view.children else None


def build_grouped_dashboard():
    """Groups online accounts BY GAME (game name as the header, every
    account currently playing it listed underneath), with offline accounts
    collected under their own section at the bottom. Returns (embed, view).
    """
    embed = discord.Embed(title="🎮 Live Dashboard", color=0xFF8C28)
    if not accounts:
        embed.description = "No accounts reporting yet."
        return embed, None

    now = time.time()
    by_game = {}
    offline_lines = []
    join_entries = []

    for label, data in sorted(accounts.items()):
        elapsed = now - data["lastSeen"]
        online = is_account_online(data, now)
        if online:
            game = data.get("gameName") or "Unknown"
            by_game.setdefault(game, []).append((label, elapsed))
            join_entries.append((label, data.get("placeId"), data.get("jobId")))
        else:
            offline_lines.append(f"**{label}** - last seen {format_elapsed(elapsed)} ago")

    for game, players in sorted(by_game.items()):
        value = "\n".join(f"🟢 **{label}** - {format_elapsed(elapsed)} ago" for label, elapsed in players)
        embed.add_field(name=f"🎮 {game}", value=value, inline=False)

    if offline_lines:
        embed.add_field(name="🔴 Offline", value="\n".join(offline_lines), inline=False)

    view = build_join_view(join_entries)
    return embed, view


def build_user_list():
    """Quick flat online/offline list, no game info - the faster-glance
    alternative to the full grouped dashboard."""
    embed = discord.Embed(title="👥 Accounts", color=0xFF8C28)
    if not accounts:
        embed.description = "No accounts reporting yet."
        return embed

    now = time.time()
    lines = []
    for label, data in sorted(accounts.items()):
        online = is_account_online(data, now)
        status = "🟢 Online" if online else "🔴 Offline"
        lines.append(f"**{label}** - {status}")
    embed.description = "\n".join(lines)
    return embed


class PanelView(discord.ui.View):
    """Buttons here use fixed custom_ids and timeout=None, which is what
    makes them keep working forever - including across bot restarts -
    once registered via bot.add_view() in on_ready, rather than being tied
    to one specific message the way the old auto-refresh loop was."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📊 Dashboard", style=discord.ButtonStyle.success, custom_id="panel_dashboard_v1")
    async def dashboard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = build_grouped_dashboard()
        if view:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="👥 User List", style=discord.ButtonStyle.secondary, custom_id="panel_userlist_v1")
    async def userlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_user_list()
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
    return web.json_response({"ok": True, "accounts": len(accounts)})


async def start_web_server():
    app = web.Application()
    app.router.add_post("/report", handle_report)
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_SERVER_PORT)
    await site.start()


_panel_view_registered = False


@bot.event
async def on_ready():
    global _panel_view_registered
    print(f"Logged in as {bot.user}", flush=True)
    # Guarded so a gateway reconnect (which re-fires on_ready) doesn't
    # register a second copy of the same persistent view.
    if not _panel_view_registered:
        bot.add_view(PanelView())
        _panel_view_registered = True


@bot.command()
async def panel(ctx):
    """Posts the persistent control panel. Its buttons keep working forever,
    even across bot restarts - you only need to run this once."""
    embed = discord.Embed(
        title="🎮 DivineSouls Control Panel",
        description="Click a button below to check your accounts.",
        color=0xFF8C28,
    )
    await ctx.send(embed=embed, view=PanelView())


@bot.command()
async def dashboard(ctx):
    """Text-command equivalent of the Dashboard button."""
    embed, view = build_grouped_dashboard()
    if view:
        await ctx.send(embed=embed, view=view)
    else:
        await ctx.send(embed=embed)


@bot.command()
async def userlist(ctx):
    """Text-command equivalent of the User List button."""
    await ctx.send(embed=build_user_list())


@bot.command()
async def remove(ctx, label: str):
    """Remove an account from the dashboard permanently (e.g. retired for good)."""
    if label in accounts:
        del accounts[label]
        await ctx.send(f"Removed `{label}` from the dashboard.")
    else:
        await ctx.send(f"No account labeled `{label}` found.")


async def main():
    discord.utils.setup_logging(level=logging.INFO)
    asyncio.create_task(start_web_server())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

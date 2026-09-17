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
MAX_JOIN_BUTTONS = 25   # Discord's hard cap on components per view (5 rows x 5)
MAX_STATUS_FIELDS = 24  # leave 1 slot free for an "+N more" notice, embeds cap at 25 fields

# ---- STATE ----
# Keyed by the account's stable Roblox userId (as a string) when the
# reporter sends one - never a manually-typed label anymore, so nothing
# needs to be kept in sync if an alt gets renamed. Falls back to playerName,
# then to a legacy "label" field, only for reports from an older reporter
# script that predates userId - keeps old accounts from erroring out or
# vanishing the moment the bot updates.
accounts = {}  # key -> {placeId, jobId, gameName, playerName, userId, lastSeen, intervalSeconds}

intents = discord.Intents.default()
intents.message_content = True  # needed for the !panel/!remove text commands
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
    return (now - data.get("lastSeen", 0)) <= data.get("intervalSeconds", 300) * OFFLINE_TIMEOUT_MULTIPLIER


def display_name(key, data):
    """Always prefer the real Roblox username. Only falls back to the
    internal key (a userId, or a legacy label for very old reports) if
    playerName is somehow missing, so nothing ever renders blank."""
    return data.get("playerName") or key


def build_join_view(entries_with_join_info):
    """entries_with_join_info: list of (displayName, placeId, jobId). Returns
    a View of link buttons, or None if there's nothing to add - link buttons
    don't need custom_id/persistence since Discord opens the URL directly
    without ever calling back into the bot."""
    view = discord.ui.View(timeout=None)
    for name, place_id, job_id in entries_with_join_info:
        if len(view.children) >= MAX_JOIN_BUTTONS:
            break
        if place_id and job_id:
            join_url = f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"
            view.add_item(discord.ui.Button(label=f"Join {name}", url=join_url))
    return view if view.children else None


def build_status_embed():
    """One field per account: what they're playing, online/offline, and how
    long since they last reported. Capped at MAX_STATUS_FIELDS so a large
    number of accounts can never exceed Discord's 25-field embed limit and
    error out - it always renders something for everyone, even if that
    means a trailing '+N more' notice instead of a crash."""
    embed = discord.Embed(title="📡 Account Status", color=0xFF8C28)
    if not accounts:
        embed.description = "No accounts reporting yet."
        return embed, None

    now = time.time()
    join_entries = []
    shown = 0
    overflow = 0

    for key, data in sorted(accounts.items(), key=lambda kv: display_name(kv[0], kv[1]).lower()):
        name = display_name(key, data)
        game = data.get("gameName") or "Unknown"
        online = is_account_online(data, now)
        elapsed = now - data.get("lastSeen", now)

        if shown < MAX_STATUS_FIELDS:
            value = (
                f"🎮 Playing: **{game}**\n"
                f"📶 Status: {'🟢 Online' if online else '🔴 Offline'}\n"
                f"🕐 Last seen: {format_elapsed(elapsed)} ago"
            )
            embed.add_field(name=f"👤 {name}", value=value, inline=False)
            shown += 1
        else:
            overflow += 1

        if online:
            join_entries.append((name, data.get("placeId"), data.get("jobId")))

    if overflow > 0:
        embed.add_field(
            name="⚠️ More accounts",
            value=f"+{overflow} more not shown (Discord's 25-field limit per message).",
            inline=False,
        )

    view = build_join_view(join_entries)
    return embed, view


def build_user_list():
    """Quick flat online/offline list, no game info - the faster-glance
    alternative to the full Status view."""
    embed = discord.Embed(title="👥 Accounts", color=0xFF8C28)
    if not accounts:
        embed.description = "No accounts reporting yet."
        return embed

    now = time.time()
    lines = []
    for key, data in sorted(accounts.items(), key=lambda kv: display_name(kv[0], kv[1]).lower()):
        name = display_name(key, data)
        online = is_account_online(data, now)
        status = "🟢 Online" if online else "🔴 Offline"
        lines.append(f"**{name}** - {status}")
    embed.description = "\n".join(lines)
    return embed


class PanelView(discord.ui.View):
    """Buttons here use fixed custom_ids and timeout=None, which is what
    makes them keep working forever - including across bot restarts -
    once registered via bot.add_view() in on_ready, rather than being tied
    to one specific message.

    TO ADD A NEW BUTTON LATER: copy one of the @discord.ui.button blocks
    below, give it a NEW unique custom_id (never reuse or remove an old
    one - people may still have an existing panel message with the old
    button visible), write its handler, done. No other changes needed
    anywhere else in the file."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📡 Status", style=discord.ButtonStyle.success, custom_id="panel_status_v2")
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = build_status_embed()
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

    # Identity priority: userId (stable, survives renames) > playerName >
    # legacy "label" field, so an older reporter script still gets stored
    # under SOMETHING instead of being rejected outright.
    user_id = data.get("userId")
    player_name = data.get("playerName")
    legacy_label = data.get("label")

    key = str(user_id) if user_id else (player_name or legacy_label)
    if not key:
        return web.json_response({"error": "missing userId/playerName/label - nothing to identify this account by"}, status=400)

    accounts[key] = {
        "placeId": data.get("placeId"),
        "jobId": data.get("jobId"),
        "gameName": data.get("gameName", "Unknown"),
        "playerName": player_name or legacy_label,
        "userId": user_id,
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
async def remove(ctx, key: str):
    """Remove an account from the dashboard permanently (e.g. retired for
    good). Use the username shown on the Status panel - or the userId if
    you need to disambiguate."""
    if key in accounts:
        del accounts[key]
        await ctx.send(f"Removed `{key}` from the dashboard.")
    else:
        await ctx.send(f"No account found for `{key}`.")


async def main():
    discord.utils.setup_logging(level=logging.INFO)
    asyncio.create_task(start_web_server())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

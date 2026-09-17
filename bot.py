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
PAGE_SIZE = 10           # accounts per Status page
MAX_EMBED_FIELDS = 24    # Discord's real cap is 25 - reserve 1 for an overflow notice, just in case

# ---- STATE ----
# Keyed by the account's stable Roblox userId (as a string) when the
# reporter sends one - never a manually-typed label. Falls back to
# playerName, then a legacy "label" field, only for reports from an older
# reporter script - keeps old accounts from erroring out mid-transition.
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
    internal key (a userId, or a legacy label) if playerName is somehow
    missing, so nothing ever renders blank."""
    return data.get("playerName") or key


def sorted_accounts():
    return sorted(accounts.items(), key=lambda kv: display_name(kv[0], kv[1]).lower())


def build_user_list():
    """Quick flat online/offline list, no game info - the faster-glance
    alternative to the full Status paginator."""
    embed = discord.Embed(title="👥 Accounts", color=0xFF8C28)
    if not accounts:
        embed.description = "No accounts reporting yet."
        return embed

    now = time.time()
    lines = []
    for key, data in sorted_accounts():
        name = display_name(key, data)
        online = is_account_online(data, now)
        status = "🟢 Online" if online else "🔴 Offline"
        lines.append(f"**{name}** - {status}")
    embed.description = "\n".join(lines)
    return embed


class StatusView(discord.ui.View):
    """A per-session paginator for the Status embed: PAGE_SIZE accounts per
    page, grouped by game (so the game name is only shown once per group
    instead of repeated per account), with a <<  <  🔄  >  >> pager row
    matching the reference layout. Join buttons only ever cover the
    accounts actually visible on the CURRENT page, so the button count
    scales with the page size instead of the total account count.

    This view belongs to one ephemeral Status response, so a plain timeout
    (not a persistent custom_id) is correct here - unlike the top-level
    PanelView buttons, which need to keep working forever."""

    def __init__(self, invoker_id):
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.page = 0
        self.rebuild()

    def max_page(self):
        total = len(accounts)
        return max(0, (total - 1) // PAGE_SIZE) if total else 0

    def get_page_entries(self):
        entries = sorted_accounts()
        if not entries:
            return []
        self.page = max(0, min(self.page, self.max_page()))
        start = self.page * PAGE_SIZE
        return entries[start:start + PAGE_SIZE]

    def build_embed(self):
        total = len(accounts)
        embed = discord.Embed(title="📡 Account Status", color=0xFF8C28)
        if total == 0:
            embed.description = "No accounts reporting yet."
            return embed

        page_entries = self.get_page_entries()
        now = time.time()

        by_game = {}
        order = []
        for key, data in page_entries:
            game = data.get("gameName") or "Unknown"
            by_game.setdefault(game, []).append((key, data))
            if game not in order:
                order.append(game)

        fields_used = 0
        for game in order:
            group = by_game[game]
            # +1 for this game's own header field, so we never add a header
            # with nowhere to put its accounts.
            if fields_used + 1 + len(group) > MAX_EMBED_FIELDS:
                remaining = sum(len(by_game[g]) for g in order[order.index(game):])
                embed.add_field(name="⚠️ More on this page", value=f"+{remaining} accounts not shown here.", inline=False)
                break

            embed.add_field(name=f"🎮 {game}", value="\u200b", inline=False)
            fields_used += 1
            for key, data in group:
                name = display_name(key, data)
                online = is_account_online(data, now)
                elapsed = now - data.get("lastSeen", now)
                # Status + last seen combined onto one line, side-by-side
                # (inline=True) with other accounts instead of each taking a
                # full-width row - this is what actually fills the empty
                # right-hand space Discord otherwise leaves on a lone
                # inline=False text field.
                embed.add_field(
                    name=f"👤 {name}",
                    value=f"{'🟢 Online' if online else '🔴 Offline'} • {format_elapsed(elapsed)} ago",
                    inline=True,
                )
                fields_used += 1

        embed.set_footer(text=f"Page {self.page + 1} of {self.max_page() + 1} • {total} account(s) total")
        return embed

    def rebuild(self):
        self.clear_items()
        total_pages = self.max_page() + 1
        at_first = self.page <= 0
        at_last = self.page >= total_pages - 1

        def make_callback(target_page_fn):
            async def callback(interaction: discord.Interaction):
                if interaction.user.id != self.invoker_id:
                    await interaction.response.send_message(
                        "This isn't your Status panel - click the Status button yourself to get your own.",
                        ephemeral=True,
                    )
                    return
                self.page = max(0, min(target_page_fn(), self.max_page()))
                self.rebuild()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return callback

        first_btn = discord.ui.Button(emoji="⏮️", style=discord.ButtonStyle.secondary, disabled=at_first, row=0)
        prev_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary, disabled=at_first, row=0)
        refresh_btn = discord.ui.Button(emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
        next_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary, disabled=at_last, row=0)
        last_btn = discord.ui.Button(emoji="⏭️", style=discord.ButtonStyle.secondary, disabled=at_last, row=0)

        first_btn.callback = make_callback(lambda: 0)
        prev_btn.callback = make_callback(lambda: self.page - 1)
        refresh_btn.callback = make_callback(lambda: self.page)
        next_btn.callback = make_callback(lambda: self.page + 1)
        last_btn.callback = make_callback(lambda: self.max_page())

        for b in (first_btn, prev_btn, refresh_btn, next_btn, last_btn):
            self.add_item(b)

        # Join buttons for THIS PAGE's online accounts only (rows 1-3, so
        # combined with the row-0 pager we stay safely under Discord's
        # 5-row / 25-button hard cap even at a full 10-account page).
        now = time.time()
        row, col = 1, 0
        for key, data in self.get_page_entries():
            if not is_account_online(data, now):
                continue
            place_id, job_id = data.get("placeId"), data.get("jobId")
            if not (place_id and job_id):
                continue
            name = display_name(key, data)
            url = f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"
            self.add_item(discord.ui.Button(label=f"Join {name}", url=url, row=row))
            col += 1
            if col >= 5:
                col, row = 0, row + 1
                if row > 4:
                    break  # hard safety stop - never exceed Discord's row limit


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
        view = StatusView(invoker_id=interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

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

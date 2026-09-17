import asyncio
import base64
import hmac
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

# ---- CONFIG ----
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DASHBOARD_CHANNEL_ID = int(os.environ["DASHBOARD_CHANNEL_ID"])
REPORT_SECRET = os.environ["REPORT_SECRET"]
# Separate from REPORT_SECRET on purpose: this one gets typed into a phone
# browser and stored in localStorage for the PWA dashboard, so it shouldn't
# be the same secret your Roblox reporters use to write data.
DASHBOARD_KEY = os.environ["DASHBOARD_KEY"]

OFFLINE_TIMEOUT_MULTIPLIER = 2
WEB_SERVER_PORT = int(os.environ.get("PORT", 8080))
PAGE_SIZE = 10           # accounts per Status page
MAX_EMBED_FIELDS = 24    # Discord's real cap is 25 - reserve 1 for an overflow notice, just in case

# Where the dashboard is persisted between restarts. NOTE: on Render's free
# tier there is no persistent disk, so this file only survives a crash that
# restarts the SAME container - it will NOT survive a redeploy or a free-tier
# spin-down that provisions a fresh container. A paid plan + attached disk
# is required for that. Still strictly better than pure in-memory state.
DATA_FILE = Path(os.environ.get("DATA_FILE", "accounts.json"))

# ---- BRAND / UI ----
# One accent color used everywhere so every embed reads as the same product
# instead of a pile of ad-hoc commands. Green/red are reserved for
# online/offline signal so they stay meaningful instead of decorative.
COLOR_PRIMARY = 0xFF8C28
COLOR_ONLINE = 0x57F287
FOOTER_TEXT = "DivineSouls Dashboard"

# ---- STATE ----
# Keyed by the account's stable Roblox userId (as a string) when the
# reporter sends one - never a manually-typed label. Falls back to
# playerName, then a legacy "label" field, only for reports from an older
# reporter script - keeps old accounts from erroring out mid-transition.
accounts = {}  # key -> {placeId, jobId, gameName, playerName, userId, lastSeen, intervalSeconds}

# Slash commands don't need the privileged message_content intent at all.
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

_panel_view_registered = False


# ---- PERSISTENCE ----
def load_accounts():
    global accounts
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r") as f:
                accounts = json.load(f)
            print(f"Loaded {len(accounts)} account(s) from {DATA_FILE}", flush=True)
        except Exception as e:
            print(f"Could not read {DATA_FILE} ({e}) - starting with an empty dashboard.", flush=True)
            accounts = {}
    else:
        accounts = {}


def save_accounts():
    """Atomic write: write to a temp file then rename over the real one, so
    a crash mid-write can never leave accounts.json half-written/corrupt -
    a rename is atomic on POSIX, a direct write to the destination isn't."""
    try:
        tmp_path = DATA_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(accounts, f)
        tmp_path.replace(DATA_FILE)
    except Exception as e:
        print(f"Failed to save accounts to {DATA_FILE}: {e}", flush=True)


# ---- HELPERS ----
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


def styled_embed(title, color=COLOR_PRIMARY, description=None):
    """Every embed in the bot goes through this so title casing, footer,
    and timestamp stay consistent instead of copy-pasted per command."""
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    if description:
        embed.description = description
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def build_user_list():
    """Quick flat online/offline list, no game info - the faster-glance
    alternative to the full Status paginator."""
    embed = styled_embed("👥 Accounts")
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


def build_summary_embed():
    now = time.time()
    total = len(accounts)
    online = sum(1 for _, data in accounts.items() if is_account_online(data, now))
    offline = total - online

    embed = styled_embed("📊 Summary")
    embed.add_field(name="Total", value=str(total), inline=True)
    embed.add_field(name="🟢 Online", value=str(online), inline=True)
    embed.add_field(name="🔴 Offline", value=str(offline), inline=True)
    return embed


def build_panel_embed():
    embed = styled_embed(
        "🎮 DivineSouls Control Panel",
        description="Click a button below to check your accounts.",
    )
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
        embed = styled_embed("📡 Account Status")
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

        embed.set_footer(text=f"{FOOTER_TEXT} • Page {self.page + 1} of {self.max_page() + 1} • {total} account(s)")
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

    @discord.ui.button(label="📊 Summary", style=discord.ButtonStyle.primary, custom_id="panel_summary_v1")
    async def summary_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_summary_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---- SLASH COMMANDS (restricted to DASHBOARD_CHANNEL_ID) ----
def in_dashboard_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != DASHBOARD_CHANNEL_ID:
            await interaction.response.send_message(
                f"This command only works in <#{DASHBOARD_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


@bot.tree.command(name="panel", description="Post the persistent DivineSouls control panel in this channel.")
@in_dashboard_channel()
async def panel_command(interaction: discord.Interaction):
    """Posts the persistent control panel. Its buttons keep working forever,
    even across bot restarts - you only need to run this once."""
    await interaction.response.send_message(embed=build_panel_embed(), view=PanelView())


@bot.tree.command(name="remove", description="Remove an account from the dashboard.")
@app_commands.describe(key="The account to remove (pick from the list, or type a username/userId)")
@in_dashboard_channel()
async def remove_command(interaction: discord.Interaction, key: str):
    """Remove an account from the dashboard permanently (e.g. retired for
    good). Use the username shown on the Status panel - or the userId if
    you need to disambiguate."""
    if key in accounts:
        del accounts[key]
        save_accounts()
        await interaction.response.send_message(f"Removed **{key}** from the dashboard.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No account found for `{key}`.", ephemeral=True)


@remove_command.autocomplete("key")
async def remove_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    choices = []
    for key, data in sorted_accounts():
        name = display_name(key, data)
        if current in name.lower() or current in key.lower():
            choices.append(app_commands.Choice(name=name, value=key))
        if len(choices) >= 25:  # Discord's hard cap on autocomplete choices
            break
    return choices


# ---- PWA (installable phone dashboard) ----
# Read-only view of the same data the Discord embeds show, served as an
# installable web app so it gets its own home-screen icon on Android/iOS
# without needing Discord open. Protected by DASHBOARD_KEY (not
# REPORT_SECRET) since this key lives in a browser's localStorage.
ICON_192_B64 = "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"
ICON_512_B64 = "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"
MANIFEST_JSON = json.dumps({
    "name": "DivineSouls Dashboard",
    "short_name": "DSouls",
    "start_url": "/dashboard",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0d0d0f",
    "theme_color": "#0d0d0f",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
})

SW_JS = """
// Bump this on every deploy that changes PWA_HTML/manifest/icons. The old
// cache-first strategy meant an installed PWA could get permanently stuck
// on the HTML it first cached, drifting out of sync with what a plain
// browser tab (which just hits the network) shows. Network-first for the
// shell below fixes that; bumping the name here also forces any previously
// installed app to drop its stale cache on this deploy.
const CACHE_NAME = "ds-dashboard-v3";
const SHELL = ["/dashboard", "/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  if (url.pathname === "/status") {
    event.respondWith(
      fetch(event.request).catch(
        () => new Response(JSON.stringify({ total: 0, online: 0, offline: 0, accounts: [] }), {
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    return;
  }

  // Network-first for the app shell (HTML/manifest/icons) so the installed
  // PWA always shows what's currently deployed - same as a fresh browser
  // tab would - and only falls back to the cached copy when offline.
  if (SHELL.includes(url.pathname)) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
"""

PWA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>DivineSouls Dashboard</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="theme-color" content="#0d0d0f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<script src="https://cdn.tailwindcss.com"></script>
<script>
  // Brand tokens live here instead of a CSS :root block, so every Tailwind
  // utility class (bg-accent, text-online, border-borderc, etc.) below maps
  // straight back to the bot's COLOR_PRIMARY / COLOR_ONLINE constants.
  tailwind.config = {
    theme: {
      extend: {
        colors: {
          accent: "#FF8C28",
          accent2: "#c9631a",
          bgmain: "#0d0d0f",
          sidebar: "#111113",
          card: "#17171a",
          borderc: "#26262a",
          muted: "#8a8a90",
          online: "#57F287",
          offline: "#ED4245",
        },
      },
    },
  };
</script>
<style>
  * { -webkit-tap-highlight-color: transparent; }
  /* Installed, fullscreen PWA only (not a normal browser tab, which
     already has its own chrome for this): pad for the notch/status bar
     so content doesn't sit under it. */
  @media (display-mode: standalone) {
    body { padding-top: env(safe-area-inset-top); }
  }
</style>
</head>
<body class="m-0 min-h-screen bg-bgmain text-[#f2f2f2] font-sans flex flex-col">

<div id="keyGate" class="fixed inset-0 bg-bgmain flex-col items-center justify-center gap-3.5 p-6 z-20 hidden">
  <div class="flex items-center gap-2.5 mb-1">
    <div class="w-9 h-9 rounded-[9px] bg-gradient-to-br from-accent to-accent2 flex items-center justify-center font-extrabold text-sm text-[#1a1005]">DS</div>
  </div>
  <h1 class="text-lg m-0">DivineSouls <span class="text-accent">Dashboard</span></h1>
  <p class="text-muted text-[13px] text-center max-w-[260px]">Enter your dashboard key (set as DASHBOARD_KEY on the bot) to view account status.</p>
  <input id="keyInput" type="password" placeholder="Dashboard key" autocomplete="off"
    class="bg-card border border-borderc text-[#f2f2f2] px-3.5 py-3 rounded-[10px] text-[15px] w-full max-w-[280px] outline-none focus:border-accent">
  <button id="keySubmit" class="bg-accent text-[#1a1005] font-bold border-none px-5 py-3 rounded-[10px] text-[15px] cursor-pointer">Unlock</button>
</div>

<div id="app" class="hidden flex-1 min-h-screen">
  <div class="flex w-full">

    <div class="sidebar fixed bottom-0 inset-x-0 md:relative md:inset-auto md:w-[220px] flex-shrink-0 bg-sidebar border-t md:border-t-0 md:border-r border-borderc p-1.5 md:p-[18px_12px] flex flex-row md:flex-col gap-0 md:gap-[22px] z-[15]">
      <div class="hidden md:flex items-center gap-2.5 px-1.5">
        <div class="w-[34px] h-[34px] rounded-[9px] bg-gradient-to-br from-accent to-accent2 flex items-center justify-center font-extrabold text-[13px] text-[#1a1005] flex-shrink-0">DS</div>
        <div>
          <div class="font-bold text-sm tracking-wide">DIVINESOULS</div>
          <div class="text-[11px] text-muted">Account Dashboard</div>
        </div>
      </div>

      <div class="flex flex-row md:flex-col flex-1 md:flex-none gap-1 md:gap-0">
        <div class="hidden md:block text-[10px] uppercase tracking-wide text-muted px-2.5 pb-2">Monitor</div>
        <div class="navitem flex-1 md:flex-none flex flex-col md:flex-row items-center justify-center md:justify-between gap-0.5 md:gap-0 px-1 md:px-2.5 py-1.5 md:py-2.5 rounded-lg text-[11px] md:text-sm cursor-pointer border-t-2 md:border-t-0 md:border-l-2 mb-0 md:mb-0.5 border-accent bg-[#1e1a14] text-accent" data-filter="all">
          <span>Fleet</span><span class="count text-[10.5px] md:text-[11.5px] text-muted" id="navAll">0</span>
        </div>
        <div class="navitem flex-1 md:flex-none flex flex-col md:flex-row items-center justify-center md:justify-between gap-0.5 md:gap-0 px-1 md:px-2.5 py-1.5 md:py-2.5 rounded-lg text-[11px] md:text-sm cursor-pointer border-t-2 md:border-t-0 md:border-l-2 mb-0 md:mb-0.5 border-transparent text-[#cfcfd2]" data-filter="online">
          <span>Online</span><span class="count text-[10.5px] md:text-[11.5px] text-muted" id="navOnline">0</span>
        </div>
        <div class="navitem flex-1 md:flex-none flex flex-col md:flex-row items-center justify-center md:justify-between gap-0.5 md:gap-0 px-1 md:px-2.5 py-1.5 md:py-2.5 rounded-lg text-[11px] md:text-sm cursor-pointer border-t-2 md:border-t-0 md:border-l-2 mb-0 md:mb-0.5 border-transparent text-[#cfcfd2]" data-filter="offline">
          <span>Offline</span><span class="count text-[10.5px] md:text-[11.5px] text-muted" id="navOffline">0</span>
        </div>
      </div>

      <div class="hidden md:block">
        <div class="text-[10px] uppercase tracking-wide text-muted px-2.5 pb-2">Account</div>
        <div id="resetKeyNav" class="flex items-center justify-between px-2.5 py-2.5 rounded-lg text-sm text-[#cfcfd2] cursor-pointer hover:bg-[#1b1b1e]">
          <span>Dashboard key</span>
        </div>
      </div>
    </div>

    <div class="main flex-1 min-w-0 px-4 md:px-[22px] pt-4 md:pt-[18px] pb-[calc(84px+env(safe-area-inset-bottom))] md:pb-10">
      <div class="max-w-[900px] mx-auto w-full">
        <div class="flex items-center gap-3.5 flex-wrap pb-4 border-b border-borderc mb-[18px]">
          <div class="flex gap-1.5 flex-wrap" id="gameTabs"></div>
          <div class="flex-1"></div>
          <div class="flex items-center gap-1.5 text-[12.5px] text-muted">
            <span class="w-[7px] h-[7px] rounded-full bg-online shadow-[0_0_5px_#57F287]"></span>Live
          </div>
          <div class="text-xs text-[#5c5c62]" id="updatedText">updated just now</div>
          <button id="refreshBtn" title="Refresh" class="bg-card border border-borderc text-muted text-[15px] px-2.5 py-1.5 rounded-lg cursor-pointer">&#8635;</button>
        </div>

        <div class="flex gap-2.5 mb-[18px]">
          <div class="flex-1 bg-card border border-borderc rounded-[14px] px-2.5 py-3.5 text-center">
            <div class="text-[22px] font-bold text-accent" id="numTotal">-</div>
            <div class="text-[11px] text-muted mt-0.5 uppercase tracking-wide">Total</div>
          </div>
          <div class="flex-1 bg-card border border-borderc rounded-[14px] px-2.5 py-3.5 text-center">
            <div class="text-[22px] font-bold text-online" id="numOnline">-</div>
            <div class="text-[11px] text-muted mt-0.5 uppercase tracking-wide">Online</div>
          </div>
          <div class="flex-1 bg-card border border-borderc rounded-[14px] px-2.5 py-3.5 text-center">
            <div class="text-[22px] font-bold text-offline" id="numOffline">-</div>
            <div class="text-[11px] text-muted mt-0.5 uppercase tracking-wide">Offline</div>
          </div>
        </div>

        <div id="list" class="flex flex-col gap-2"></div>

        <footer class="text-center text-[#4a4a4f] text-[11px] pt-5 pb-1">
          Auto-refreshes every 15s &middot; <button id="resetKey" class="bg-transparent border-none text-[#4a4a4f] underline text-[11px] cursor-pointer">reset key</button>
        </footer>
      </div>
    </div>
  </div>
</div>

<script>
const STORAGE_KEY = "ds_dashboard_key";
const gate = document.getElementById("keyGate");
const app = document.getElementById("app");
const list = document.getElementById("list");
const gameTabsEl = document.getElementById("gameTabs");

let currentFilter = "all";   // all | online | offline
let currentGame = "all";     // "all" or a specific game name
let lastData = null;
let lastUpdatedAt = null;

// Tailwind utility classes swapped in/out for active vs inactive nav items,
// since these are toggled at runtime rather than rebuilt like the game tabs.
const NAV_ACTIVE = ["border-accent", "bg-[#1e1a14]", "text-accent"];
const NAV_INACTIVE = ["border-transparent", "text-[#cfcfd2]"];

function setNavActive(el, isActive) {
  el.classList.remove(...NAV_ACTIVE, ...NAV_INACTIVE);
  el.classList.add(...(isActive ? NAV_ACTIVE : NAV_INACTIVE));
}

function formatElapsed(s) {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60), rm = m % 60;
  return `${h}h ${rm}m`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function updateAgoText() {
  if (!lastUpdatedAt) return;
  const secs = Math.floor((Date.now() - lastUpdatedAt) / 1000);
  document.getElementById("updatedText").textContent =
    secs < 3 ? "updated just now" : `updated ${formatElapsed(secs)} ago`;
}
setInterval(updateAgoText, 1000);

function buildGameTabs(accounts) {
  const counts = {};
  accounts.forEach((a) => { counts[a.game] = (counts[a.game] || 0) + 1; });
  const games = Object.keys(counts).sort();

  if (currentGame !== "all" && !counts[currentGame]) currentGame = "all";

  const tabs = [{ key: "all", label: "All games", count: accounts.length }]
    .concat(games.map((g) => ({ key: g, label: g, count: counts[g] })));

  gameTabsEl.innerHTML = tabs.map((t) => {
    const active = t.key === currentGame;
    const base = "flex items-center gap-1.5 text-[12.5px] px-3 py-1.5 rounded-lg cursor-pointer border";
    const state = active
      ? "bg-[#211c15] border-[#4a3316] text-accent"
      : "bg-card border-borderc text-muted";
    const badgeState = active ? "bg-accent text-[#1a1005]" : "bg-[#2a2a2e] text-[#d5d5d8]";
    return `
      <div class="tab ${base} ${state}" data-game="${escapeHtml(t.key)}">
        ${escapeHtml(t.label)} <span class="text-[10.5px] px-1.5 py-0.5 rounded-full ${badgeState}">${t.count}</span>
      </div>
    `;
  }).join("");

  gameTabsEl.querySelectorAll(".tab").forEach((el) => {
    el.addEventListener("click", () => {
      currentGame = el.getAttribute("data-game");
      render(lastData);
    });
  });
}

function render(data) {
  lastData = data;
  lastUpdatedAt = Date.now();
  updateAgoText();

  document.getElementById("numTotal").textContent = data.total;
  document.getElementById("numOnline").textContent = data.online;
  document.getElementById("numOffline").textContent = data.offline;
  document.getElementById("navAll").textContent = data.total;
  document.getElementById("navOnline").textContent = data.online;
  document.getElementById("navOffline").textContent = data.offline;

  const accounts = data.accounts || [];
  buildGameTabs(accounts);

  const filtered = accounts.filter((a) => {
    const matchesStatus =
      currentFilter === "all" ? true : currentFilter === "online" ? a.online : !a.online;
    const matchesGame = currentGame === "all" ? true : a.game === currentGame;
    return matchesStatus && matchesGame;
  });

  if (filtered.length === 0) {
    list.innerHTML = `<div class="text-center text-muted py-[60px] px-5 text-sm">No accounts match this view.</div>`;
    return;
  }

  list.innerHTML = filtered.map((a) => {
    const dotClass = a.online ? "bg-online shadow-[0_0_6px_#57F287]" : "bg-offline";
    const statusText = a.online ? "online" : formatElapsed(a.lastSeenSecondsAgo) + " ago";
    const joinBtn = a.online && a.joinUrl
      ? `<a class="bg-accent text-[#1a1005] font-bold text-xs px-3 py-2 rounded-lg no-underline flex-shrink-0" href="${a.joinUrl}">Join</a>`
      : "";
    return `
      <div class="row bg-card border border-borderc rounded-xl px-3.5 py-3 flex items-center gap-3">
        <div class="w-[9px] h-[9px] rounded-full flex-shrink-0 ${dotClass}"></div>
        <div class="flex-1 min-w-0">
          <div class="font-semibold text-[14.5px] overflow-hidden text-ellipsis whitespace-nowrap">${escapeHtml(a.name)}</div>
          <div class="text-xs text-muted mt-0.5 overflow-hidden text-ellipsis whitespace-nowrap">${escapeHtml(a.game)} &middot; ${statusText}</div>
        </div>
        ${joinBtn}
      </div>
    `;
  }).join("");
}

async function refresh() {
  const key = localStorage.getItem(STORAGE_KEY);
  if (!key) return;
  try {
    const res = await fetch("/status?key=" + encodeURIComponent(key));
    if (res.status === 401) {
      localStorage.removeItem(STORAGE_KEY);
      showGate();
      return;
    }
    const data = await res.json();
    render(data);
  } catch (e) {
    console.error("refresh failed", e);
  }
}

function showApp() {
  gate.classList.remove("flex");
  gate.classList.add("hidden");
  app.classList.remove("hidden");
  app.classList.add("flex");
  refresh();
}

function showGate() {
  gate.classList.remove("hidden");
  gate.classList.add("flex");
  app.classList.remove("flex");
  app.classList.add("hidden");
}

document.getElementById("keySubmit").addEventListener("click", () => {
  const val = document.getElementById("keyInput").value.trim();
  if (!val) return;
  localStorage.setItem(STORAGE_KEY, val);
  showApp();
});

document.getElementById("refreshBtn").addEventListener("click", refresh);
document.getElementById("resetKey").addEventListener("click", () => {
  localStorage.removeItem(STORAGE_KEY);
  showGate();
});
document.getElementById("resetKeyNav").addEventListener("click", () => {
  localStorage.removeItem(STORAGE_KEY);
  showGate();
});

document.querySelectorAll(".navitem[data-filter]").forEach((el) => {
  el.addEventListener("click", () => {
    document.querySelectorAll(".navitem[data-filter]").forEach((n) => setNavActive(n, false));
    setNavActive(el, true);
    currentFilter = el.getAttribute("data-filter");
    render(lastData || { total: 0, online: 0, offline: 0, accounts: [] });
  });
});

if (localStorage.getItem(STORAGE_KEY)) {
  showApp();
} else {
  showGate();
}

setInterval(refresh, 15000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((e) => console.error("SW register failed", e));
}
</script>
</body>
</html>
"""


async def handle_status(request):
    if not hmac.compare_digest(request.query.get("key", ""), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    now = time.time()
    out = []
    for key, data in sorted_accounts():
        online = is_account_online(data, now)
        place_id, job_id = data.get("placeId"), data.get("jobId")
        out.append({
            "key": key,
            "name": display_name(key, data),
            "online": online,
            "game": data.get("gameName") or "Unknown",
            "lastSeenSecondsAgo": int(now - data.get("lastSeen", now)),
            "joinUrl": (
                f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"
                if online and place_id and job_id else None
            ),
        })

    total = len(out)
    online_count = sum(1 for a in out if a["online"])
    return web.json_response({
        "total": total,
        "online": online_count,
        "offline": total - online_count,
        "accounts": out,
    })


async def handle_dashboard(request):
    return web.Response(text=PWA_HTML, content_type="text/html")


async def handle_manifest(request):
    return web.Response(text=MANIFEST_JSON, content_type="application/manifest+json")


async def handle_sw(request):
    return web.Response(text=SW_JS, content_type="application/javascript")


async def handle_icon_192(request):
    return web.Response(body=base64.b64decode(ICON_192_B64), content_type="image/png")


async def handle_icon_512(request):
    return web.Response(body=base64.b64decode(ICON_512_B64), content_type="image/png")


# ---- HTTP endpoints ----
async def handle_report(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    # Constant-time comparison so response timing can't leak how many
    # leading characters of the secret were correct.
    if not hmac.compare_digest(str(data.get("secret", "")), REPORT_SECRET):
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
    save_accounts()
    return web.json_response({"ok": True})


async def handle_health(request):
    return web.json_response({"ok": True, "accounts": len(accounts)})


async def start_web_server():
    app = web.Application()
    app.router.add_post("/report", handle_report)
    app.router.add_get("/", handle_health)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get("/sw.js", handle_sw)
    app.router.add_get("/icon-192.png", handle_icon_192)
    app.router.add_get("/icon-512.png", handle_icon_512)
    app.router.add_get("/status", handle_status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_SERVER_PORT)
    await site.start()


@bot.event
async def setup_hook():
    # Sync the slash command tree once at startup. This talks to Discord's
    # API, so it's here (called once by discord.py before login finishes)
    # rather than in on_ready, which can re-fire on every gateway reconnect.
    await bot.tree.sync()


@bot.event
async def on_ready():
    global _panel_view_registered
    print(f"Logged in as {bot.user}", flush=True)
    # Guarded so a gateway reconnect (which re-fires on_ready) doesn't
    # register a second copy of the same persistent view.
    if not _panel_view_registered:
        bot.add_view(PanelView())
        _panel_view_registered = True


async def main():
    discord.utils.setup_logging(level=logging.INFO)
    load_accounts()
    asyncio.create_task(start_web_server())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

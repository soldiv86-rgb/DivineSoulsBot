import asyncio
import base64
import hmac
import io
import json
import logging
import os
import re
import time
from pathlib import Path

from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

# Needed to draw the default app icon in the current accent color. If Pillow
# isn't installed, the app falls back to a fixed built-in icon that does NOT
# follow the accent color. Add "Pillow" to requirements.txt to keep the icon
# in sync with the accent.
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

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

# Same persistence caveat as DATA_FILE above applies here too.
THEME_FILE = Path(os.environ.get("THEME_FILE", "theme.json"))

# ---- BRAND / UI ----
# One accent color used everywhere so every embed reads as the same product
# instead of a pile of ad-hoc commands. Green/red are reserved for
# online/offline signal so they stay meaningful instead of decorative.
COLOR_PRIMARY = 0xFF8C28
COLOR_ONLINE = 0x57F287
FOOTER_TEXT = "DivineSoul Dashboard"

# ---- STATE ----
# Keyed by the account's stable Roblox userId (as a string) when the
# reporter sends one - never a manually-typed label. Falls back to
# playerName, then a legacy "label" field, only for reports from an older
# reporter script - keeps old accounts from erroring out mid-transition.
accounts = {}  # key -> {placeId, jobId, gameName, playerName, userId, lastSeen, intervalSeconds}

# The user only ever controls two things: the accent color, and light vs
# dark mode. Everything else (backgrounds, borders, muted text, the
# gradient's second stop) is DERIVED from those two - see resolve_theme()
# below - so there's no way to end up with an inconsistent palette.
DEFAULT_THEME = {
    "accent": "#FF8C28",
    "mode": "dark",
}
theme = dict(DEFAULT_THEME)  # overwritten by load_theme() at startup if a saved theme exists

# Fixed structural colors per mode. accent/accent2 are layered on top of
# these by resolve_theme() - keep this in sync with the JS copies in
# PWA_HTML (tailwind.config + the :root defaults) if you ever tweak it.
PALETTES = {
    "dark": {
        "bgmain": "#0d0d0f",
        "sidebar": "#111113",
        "card": "#17171a",
        "borderc": "#26262a",
        "muted": "#8a8a90",
        "text": "#f2f2f2",
    },
    "light": {
        "bgmain": "#f5f5f7",
        "sidebar": "#ffffff",
        "card": "#ffffff",
        "borderc": "#e2e2e6",
        "muted": "#6b6b70",
        "text": "#141414",
    },
}
# Status colors stay constant across modes - they're semantic (green/red
# always mean online/offline), not decorative, so they shouldn't shift.
STATUS_COLORS = {"online": "#57F287", "offline": "#ED4245"}


def darken_hex(hex_color: str, factor: float = 0.3) -> str:
    """Used to derive accent2 (the second stop of the brand gradient) from
    the single accent color the user actually picks, instead of asking
    them to manage two related colors by hand."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r, g, b = (max(0, int(c * (1 - factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def contrast_text_color(hex_color: str) -> str:
    """Picks readable text/glyph color for a given background color using
    relative luminance, so a very light user-chosen accent doesn't end up
    with unreadable pale-on-pale icon text."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#1a1005" if luminance > 0.6 else "#ffffff"


def blend_hex(fg_hex: str, bg_hex: str, alpha: float) -> str:
    """Alpha-blends fg over bg (both #rrggbb) and returns the flattened
    #rrggbb result. Used to derive a low-opacity "accent tint" for active
    nav/tab backgrounds that stays readable in BOTH light and dark mode,
    instead of a single hardcoded dark color that only worked for dark
    mode (see accentTint in resolve_theme())."""
    fg_hex, bg_hex = fg_hex.lstrip("#"), bg_hex.lstrip("#")
    fr, fg_, fb = int(fg_hex[0:2], 16), int(fg_hex[2:4], 16), int(fg_hex[4:6], 16)
    br, bg_, bb = int(bg_hex[0:2], 16), int(bg_hex[2:4], 16), int(bg_hex[4:6], 16)
    r = round(fr * alpha + br * (1 - alpha))
    g = round(fg_ * alpha + bg_ * (1 - alpha))
    b = round(fb * alpha + bb * (1 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"


def resolve_theme() -> dict:
    """The full palette the frontend actually renders with: the user's
    accent plus a derived accent2, layered over whichever mode's fixed
    palette is active. This is what GET /theme and every POST /theme*
    response return."""
    accent = theme.get("accent", DEFAULT_THEME["accent"])
    mode = theme.get("mode", DEFAULT_THEME["mode"])
    palette = PALETTES.get(mode, PALETTES["dark"])
    return {
        "accent": accent,
        "accent2": darken_hex(accent, 0.3),
        # A low-opacity accent tint blended over THIS mode's sidebar color,
        # so "active" nav items/tabs read correctly in both light and dark
        # mode instead of a single hardcoded dark color (previously
        # #1e1a14, which only ever looked right in dark mode).
        "accentTint": blend_hex(accent, palette["sidebar"], 0.14),
        "mode": mode,
        **palette,
        **STATUS_COLORS,
    }

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


def load_theme():
    global theme
    if THEME_FILE.exists():
        try:
            with open(THEME_FILE, "r") as f:
                saved = json.load(f)
            # Merge over defaults rather than replacing outright, and drop
            # any key that isn't accent/mode - older theme.json files from
            # before this simplification may still have the old per-color
            # keys, which should just be ignored rather than resurrected.
            theme = {**DEFAULT_THEME, **{k: v for k, v in saved.items() if k in DEFAULT_THEME}}
            if theme.get("mode") not in PALETTES:
                theme["mode"] = DEFAULT_THEME["mode"]
            print(f"Loaded custom theme from {THEME_FILE}", flush=True)
        except Exception as e:
            print(f"Could not read {THEME_FILE} ({e}) - using default theme.", flush=True)
            theme = dict(DEFAULT_THEME)
    else:
        theme = dict(DEFAULT_THEME)


def save_theme():
    try:
        tmp_path = THEME_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(theme, f)
        tmp_path.replace(THEME_FILE)
    except Exception as e:
        print(f"Failed to save theme to {THEME_FILE}: {e}", flush=True)




def generate_default_icon(size: int, accent_hex: str) -> bytes:
    """The app's icon: a rounded square filled ENTIRELY with the current accent color
    - not a two-tone gradient - so changing the accent recolors the whole
    icon, and that's exactly what gets served for /icon-192.png/512.png,
    which is what the manifest points the "Add to Home Screen" icon at."""
    hex_color = accent_hex.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)

    img = Image.new("RGBA", (size, size), (r, g, b, 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=int(size * 0.22), fill=255)
    img.putalpha(mask)

    draw = ImageDraw.Draw(img)
    text_color = contrast_text_color(accent_hex)
    text = "DS"
    try:
        font = ImageFont.load_default(size=int(size * 0.42))
    except TypeError:
        # Older Pillow: load_default() doesn't take a size argument.
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), text, font=font, fill=text_color)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
        "🎮 DivineSoul Control Panel",
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


@bot.tree.command(name="panel", description="Post the persistent DivineSoul control panel in this channel.")
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
ICON_VERSION = "1"  # bump this if you swap in new icon artwork
ICON_192_B64 = "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"
ICON_512_B64 = "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"
def get_icon_version() -> str:
    """A short fingerprint used as a ?v= query param on the manifest's icon
    URLs, so that whenever the served icon bytes actually change, Chrome/
    Android is forced to re-fetch it instead of reusing a cached copy for
    that same "/icon-192.png" URL.

    When Pillow is available the icon is generated from the saved accent
    color, so the version includes that accent. Without Pillow we fall
    back to the fixed base64 logos and only ICON_VERSION matters - bump
    that if you swap in new fixed artwork.

    This does NOT fix iOS. Safari's "Add to Home Screen" captures the
    icon once at add-time with no re-check mechanism at all - that
    platform has no API for a web app to update its own home-screen icon
    after installation, so it genuinely requires deleting and re-adding
    to pick up any icon change. That's an Apple platform limitation, not
    something fixable from this codebase."""
    if HAS_PIL:
        accent = theme.get("accent", DEFAULT_THEME["accent"]).lstrip("#").lower()
        return f"accent-{accent}-v{ICON_VERSION}"
    return f"fixed-{ICON_VERSION}"


def build_manifest_json():
    """A function rather than a fixed constant, so the manifest's splash
    colors follow whatever the user has picked in the theme settings
    instead of staying stuck on the original defaults."""
    resolved = resolve_theme()
    version = get_icon_version()
    return json.dumps({
        "name": "DivineSoul Dashboard",
        "short_name": "DS",
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "background_color": resolved["bgmain"],
        "theme_color": resolved["bgmain"],
        "icons": [
            {"src": f"/icon-192.png?v={version}", "sizes": "192x192", "type": "image/png"},
            {"src": f"/icon-512.png?v={version}", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })

SW_JS = """
// Bump this on every deploy that changes PWA_HTML/manifest/icons. The old
// cache-first strategy meant an installed PWA could get permanently stuck
// on the HTML it first cached, drifting out of sync with what a plain
// browser tab (which just hits the network) shows. Network-first for the
// shell below fixes that; bumping the name here also forces any previously
// installed app to drop its stale cache on this deploy.
const CACHE_NAME = "ds-dashboard-v5";
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
<title>DivineSoul Dashboard</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="theme-color" content="#0d0d0f" id="themeColorMeta">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<script src="https://cdn.tailwindcss.com"></script>
<script>
  // Every color below points at a CSS custom property instead of a literal
  // hex value, so changing --accent/--bgmain/etc. on :root (which the
  // Settings panel does after fetching /theme) instantly re-colors every
  // bg-accent / text-online / border-borderc utility on the page - no
  // Tailwind rebuild or page reload needed.
  tailwind.config = {
    theme: {
      extend: {
        colors: {
          accent: "var(--accent)",
          accent2: "var(--accent2)",
          accenttint: "var(--accentTint)",
          bgmain: "var(--bgmain)",
          sidebar: "var(--sidebar)",
          card: "var(--card)",
          borderc: "var(--borderc)",
          muted: "var(--muted)",
          online: "var(--online)",
          offline: "var(--offline)",
          text: "var(--text)",
        },
      },
    },
  };
</script>
<style>
  /* Defaults - overwritten on :root as soon as /theme resolves, and kept
     in sync with the DEFAULT_THEME dict in bot.py. Defining them here too
     (rather than only in JS) means the very first paint already uses the
     right colors instead of flashing the stock palette for a moment. */
  :root {
    --accent: #FF8C28;
    --accent2: #c9631a;
    --accentTint: #262119;
    --bgmain: #0d0d0f;
    --sidebar: #111113;
    --card: #17171a;
    --borderc: #26262a;
    --muted: #8a8a90;
    --online: #57F287;
    --offline: #ED4245;
    --text: #f2f2f2;
  }
  * { -webkit-tap-highlight-color: transparent; }
  /* Installed, fullscreen PWA only (not a normal browser tab, which
     already has its own chrome for this): pad for the notch/status bar
     so content doesn't sit under it. */
  @media (display-mode: standalone) {
    body { padding-top: env(safe-area-inset-top); }
  }
</style>
</head>
<body class="m-0 min-h-screen bg-bgmain text-text font-sans flex flex-col">

<div id="keyGate" class="fixed inset-0 bg-bgmain flex-col items-center justify-center gap-3.5 p-6 z-20 hidden">
  <div class="flex items-center gap-2.5 mb-1">
    <img id="gateLogo" src="/icon-192.png" alt="App icon" class="w-9 h-9 rounded-[9px] object-cover">
  </div>
  <h1 class="text-lg m-0">DivineSoul <span class="text-accent">Dashboard</span></h1>
  <p class="text-muted text-[13px] text-center max-w-[260px]">Welcome to Divine Soul Dashboard! Enter your dashboard key to view account status.</p>
  <input id="keyInput" type="password" placeholder="Dashboard key" autocomplete="off"
    class="bg-card border border-borderc text-text px-3.5 py-3 rounded-[10px] text-[15px] w-full max-w-[280px] outline-none focus:border-accent">
  <button id="keySubmit" class="bg-accent text-[#1a1005] font-bold border-none px-5 py-3 rounded-[10px] text-[15px] cursor-pointer">Unlock</button>
</div>

<div id="app" class="hidden flex-1 min-h-screen">
  <div class="flex w-full">

    <div class="sidebar fixed bottom-0 inset-x-0 md:relative md:inset-auto md:w-[220px] flex-shrink-0 bg-sidebar border-t md:border-t-0 md:border-r border-borderc p-1.5 md:p-[18px_12px] flex flex-row md:flex-col gap-0 md:gap-[22px] z-[15]">
      <div class="hidden md:flex items-center gap-2.5 px-1.5">
        <img id="brandLogo" src="/icon-192.png" alt="App icon" class="w-[34px] h-[34px] rounded-[9px] object-cover flex-shrink-0">
        <div>
          <div class="font-bold text-sm tracking-wide">DIVINESOUL</div>
          <div class="text-[11px] text-muted">Account Dashboard</div>
        </div>
      </div>

      <div class="flex flex-row md:flex-col flex-1 md:flex-none gap-1 md:gap-0">
        <div class="hidden md:block text-[10px] uppercase tracking-wide text-muted px-2.5 pb-2">Monitor</div>
        <div class="navitem flex-1 md:flex-none flex flex-col md:flex-row items-center justify-center md:justify-between gap-0.5 md:gap-0 px-1 md:px-2.5 py-1.5 md:py-2.5 rounded-lg text-[11px] md:text-sm cursor-pointer border-t-2 md:border-t-0 md:border-l-2 mb-0 md:mb-0.5 border-accent bg-accenttint text-accent" data-filter="all">
          <span>Accounts</span><span class="count text-[10.5px] md:text-[11.5px] text-muted" id="navAll">0</span>
        </div>
        <div class="navitem flex-1 md:flex-none flex flex-col md:flex-row items-center justify-center md:justify-between gap-0.5 md:gap-0 px-1 md:px-2.5 py-1.5 md:py-2.5 rounded-lg text-[11px] md:text-sm cursor-pointer border-t-2 md:border-t-0 md:border-l-2 mb-0 md:mb-0.5 border-transparent text-muted" data-filter="online">
          <span>Online</span><span class="count text-[10.5px] md:text-[11.5px] text-muted" id="navOnline">0</span>
        </div>
        <div class="navitem flex-1 md:flex-none flex flex-col md:flex-row items-center justify-center md:justify-between gap-0.5 md:gap-0 px-1 md:px-2.5 py-1.5 md:py-2.5 rounded-lg text-[11px] md:text-sm cursor-pointer border-t-2 md:border-t-0 md:border-l-2 mb-0 md:mb-0.5 border-transparent text-muted" data-filter="offline">
          <span>Offline</span><span class="count text-[10.5px] md:text-[11.5px] text-muted" id="navOffline">0</span>
        </div>
      </div>

      <div class="hidden md:block">
        <div class="text-[10px] uppercase tracking-wide text-muted px-2.5 pb-2">Account</div>
        <div id="resetKeyNav" class="flex items-center justify-between px-2.5 py-2.5 rounded-lg text-sm text-muted cursor-pointer hover:bg-[#1b1b1e]">
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
          <div class="text-xs text-muted" id="updatedText">updated just now</div>
          <button id="settingsBtn" title="Settings" class="bg-card border border-borderc text-muted text-[15px] px-2.5 py-1.5 rounded-lg cursor-pointer">&#9881;</button>
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

        <footer class="text-center text-muted text-[11px] pt-5 pb-1">
          Auto-refreshes every 15s &middot; <button id="resetKey" class="bg-transparent border-none text-muted underline text-[11px] cursor-pointer">reset key</button>
        </footer>
      </div>
    </div>
  </div>
</div>

<div id="settingsModal" class="fixed inset-0 bg-black/60 flex items-center justify-center z-30 p-4 hidden">
  <div class="bg-card border border-borderc rounded-2xl w-full max-w-md max-h-[85vh] overflow-y-auto p-5">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-base font-bold m-0">Appearance</h2>
      <button id="settingsClose" class="bg-transparent border-none text-muted text-xl cursor-pointer leading-none">&times;</button>
    </div>

    <div class="mb-5">
      <div class="text-xs uppercase tracking-wide text-muted mb-2">Theme</div>
      <div class="flex gap-2">
        <button id="modeDarkBtn" data-mode="dark" class="mode-btn flex-1 border text-sm font-semibold px-3 py-2.5 rounded-lg cursor-pointer">Dark</button>
        <button id="modeLightBtn" data-mode="light" class="mode-btn flex-1 border text-sm font-semibold px-3 py-2.5 rounded-lg cursor-pointer">Light</button>
      </div>
    </div>

    <div class="mb-2">
      <div class="text-xs uppercase tracking-wide text-muted mb-2">Accent color</div>
      <div class="flex items-center gap-3">
        <input type="color" id="accentColorInput" class="w-11 h-11 rounded-lg border border-borderc bg-transparent cursor-pointer p-0">
        <button id="themeSaveBtn" class="flex-1 bg-accent text-[#1a1005] font-bold text-sm px-3 py-2.5 rounded-lg cursor-pointer">Save accent</button>
        <button id="themeResetBtn" class="bg-transparent border border-borderc text-muted text-sm px-3 py-2.5 rounded-lg cursor-pointer">Reset</button>
      </div>
      <p id="themeStatus" class="text-xs text-muted mt-3"></p>
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
const NAV_ACTIVE = ["border-accent", "bg-accenttint", "text-accent"];
const NAV_INACTIVE = ["border-transparent", "text-muted"];

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
      ? "bg-accenttint border-accent text-accent"
      : "bg-card border-borderc text-muted";
    const badgeState = active ? "bg-accent text-[#1a1005]" : "bg-borderc text-muted";
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

// ---- Appearance settings (accent color + light/dark mode) ----
const settingsBtn = document.getElementById("settingsBtn");
const settingsModal = document.getElementById("settingsModal");
const settingsClose = document.getElementById("settingsClose");
const themeColorMeta = document.getElementById("themeColorMeta");
const brandLogo = document.getElementById("brandLogo");
const gateLogo = document.getElementById("gateLogo");
const themeSaveBtn = document.getElementById("themeSaveBtn");
const themeResetBtn = document.getElementById("themeResetBtn");
const themeStatus = document.getElementById("themeStatus");
const accentColorInput = document.getElementById("accentColorInput");
const modeButtons = document.querySelectorAll(".mode-btn");

let currentTheme = null;

function setModeButtonStyles() {
  modeButtons.forEach((btn) => {
    const active = currentTheme && btn.getAttribute("data-mode") === currentTheme.mode;
    btn.classList.toggle("bg-accent", !!active);
    btn.classList.toggle("text-[#1a1005]", !!active);
    btn.classList.toggle("border-accent", !!active);
    btn.classList.toggle("bg-transparent", !active);
    btn.classList.toggle("text-muted", !active);
    btn.classList.toggle("border-borderc", !active);
  });
}

// Pushes the resolved palette onto :root as CSS custom properties, so
// every Tailwind class that points at var(--accent)/var(--bgmain)/etc.
// re-colors instantly, updates the browser chrome color, and refreshes
// the accent swatch + which mode button looks active.
function applyTheme(t) {
  currentTheme = t;
  for (const [key, value] of Object.entries(t)) {
    if (key === "mode") continue; // not a CSS color - handled by setModeButtonStyles
    document.documentElement.style.setProperty(`--${key}`, value);
  }
  if (themeColorMeta) themeColorMeta.setAttribute("content", t.bgmain);
  if (accentColorInput && t.accent) accentColorInput.value = t.accent;
  setModeButtonStyles();
  // Always re-fetch icons so the sidebar/gate mark matches the current
  // accent (server generates the PNG from theme.accent when Pillow is on).
  refreshIconImages(t && t.accent);
}

// Cache-bust every place the icon is shown (the sidebar brand mark and the
// lock-screen mark). The icon is generated server-side from the accent
// color, so include the accent in the query string for a stable bust key
// plus a timestamp so even aggressive caches can't serve a stale file.
function refreshIconImages(accent) {
  const a = (accent || (currentTheme && currentTheme.accent) || "").replace("#", "");
  const bust = "/icon-192.png?v=" + encodeURIComponent(a) + "&t=" + Date.now();
  if (brandLogo) brandLogo.src = bust;
  if (gateLogo) gateLogo.src = bust;
}

fetch("/theme")
  .then((res) => res.json())
  .then(applyTheme)
  .catch((e) => console.error("theme load failed", e));

function openSettings() {
  if (currentTheme) applyTheme(currentTheme); // make sure the picker/toggle reflect current values
  if (themeStatus) themeStatus.textContent = "";
  settingsModal.classList.remove("hidden");
}

function closeSettings() {
  settingsModal.classList.add("hidden");
}

if (settingsBtn) settingsBtn.addEventListener("click", openSettings);
if (settingsClose) settingsClose.addEventListener("click", closeSettings);
if (settingsModal) {
  settingsModal.addEventListener("click", (e) => {
    if (e.target === settingsModal) closeSettings(); // click on the backdrop closes it
  });
}

// Light/dark applies immediately on click - it's a binary preference, not
// something that needs a separate "Save" step like the accent color.
modeButtons.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const key = localStorage.getItem(STORAGE_KEY);
    const mode = btn.getAttribute("data-mode");
    themeStatus.textContent = "Saving...";
    try {
      const res = await fetch("/theme", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, mode }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "save failed");
      applyTheme(data);
      themeStatus.textContent = "Saved.";
    } catch (e) {
      themeStatus.textContent = "Error: " + e.message;
    }
  });
});

if (themeSaveBtn) {
  themeSaveBtn.addEventListener("click", async () => {
    const key = localStorage.getItem(STORAGE_KEY);
    const accent = accentColorInput.value;
    themeStatus.textContent = "Saving...";
    try {
      const res = await fetch("/theme", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, accent }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "save failed");
      applyTheme(data);
      refreshIconImages(); // the default icon is accent-colored, so it needs to update too
      themeStatus.textContent = "Saved.";
    } catch (e) {
      themeStatus.textContent = "Error: " + e.message;
    }
  });
}

if (themeResetBtn) {
  themeResetBtn.addEventListener("click", async () => {
    const key = localStorage.getItem(STORAGE_KEY);
    themeStatus.textContent = "Resetting...";
    try {
      const res = await fetch("/theme/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "reset failed");
      applyTheme(data);
      refreshIconImages();
      themeStatus.textContent = "Reset to defaults.";
    } catch (e) {
      themeStatus.textContent = "Error: " + e.message;
    }
  });
}

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
    return web.Response(text=build_manifest_json(), content_type="application/manifest+json")


async def handle_sw(request):
    return web.Response(text=SW_JS, content_type="application/javascript")


def _icon_response(size: int) -> web.Response:
    """Serve a PNG icon. With Pillow: generate from the saved accent so the
    mark always matches the theme. Without Pillow: fixed base64 fallback.
    Cache-Control is short + must-revalidate so browsers/SW don't keep an
    old accent-colored icon after the user changes the color."""
    if HAS_PIL:
        accent = theme.get("accent", DEFAULT_THEME["accent"])
        body = generate_default_icon(size, accent)
    else:
        body = base64.b64decode(ICON_192_B64 if size <= 192 else ICON_512_B64)
    return web.Response(
        body=body,
        content_type="image/png",
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "ETag": f'"{get_icon_version()}"',
        },
    )


async def handle_icon_192(request):
    return _icon_response(192)


async def handle_icon_512(request):
    return _icon_response(512)


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


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


async def handle_get_theme(request):
    # Public/unauthenticated on purpose: colors aren't sensitive, and the
    # keyGate screen (shown before anyone enters DASHBOARD_KEY) needs the
    # custom theme too, so branding is consistent from the very first paint.
    return web.json_response(resolve_theme())


async def handle_post_theme(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not hmac.compare_digest(str(data.get("key", "")), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    # accent and mode are independent - either can be sent alone (the mode
    # toggle applies instantly without touching the saved accent, and vice
    # versa for the accent picker's Save button).
    if "accent" in data:
        accent = data["accent"]
        if not isinstance(accent, str) or not HEX_COLOR_RE.match(accent):
            return web.json_response({"error": "accent must be a #rrggbb hex color"}, status=400)
        theme["accent"] = accent

    if "mode" in data:
        mode = data["mode"]
        if mode not in PALETTES:
            return web.json_response({"error": "mode must be 'light' or 'dark'"}, status=400)
        theme["mode"] = mode

    save_theme()
    return web.json_response(resolve_theme())


async def handle_post_theme_reset(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not hmac.compare_digest(str(data.get("key", "")), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    # Only the accent is reset here - light/dark mode is a separate,
    # independent preference controlled by the mode buttons, and this
    # endpoint is wired to the "Reset" button next to Accent Color, not a
    # full theme reset. (Previously this did theme.clear() +
    # theme.update(DEFAULT_THEME), which silently reset mode back to
    # "dark" too - surprising if you were in light mode.)
    theme["accent"] = DEFAULT_THEME["accent"]
    save_theme()
    return web.json_response(resolve_theme())


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
    app.router.add_get("/theme", handle_get_theme)
    app.router.add_post("/theme", handle_post_theme)
    app.router.add_post("/theme/reset", handle_post_theme_reset)
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
    load_theme()
    asyncio.create_task(start_web_server())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

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
    "background_color": "#161616",
    "theme_color": "#FF8C28",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
})

SW_JS = """
const CACHE_NAME = "ds-dashboard-v1";
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
<meta name="theme-color" content="#FF8C28">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  :root { --accent: #FF8C28; --bg: #121212; --card: #1c1c1e; --text: #f2f2f2; --muted: #9a9a9a; --online: #57F287; --offline: #ED4245; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding-bottom: 40px; }
  header { padding: 20px 16px 12px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; margin: 0; letter-spacing: 0.3px; }
  header h1 span { color: var(--accent); }
  #refreshBtn { background: none; border: none; color: var(--muted); font-size: 20px; padding: 6px; cursor: pointer; }
  .summary { display: flex; gap: 10px; padding: 0 16px 16px; }
  .chip { flex: 1; background: var(--card); border-radius: 14px; padding: 14px 10px; text-align: center; }
  .chip .num { font-size: 22px; font-weight: 700; }
  .chip .lbl { font-size: 11px; color: var(--muted); margin-top: 2px; text-transform: uppercase; letter-spacing: 0.5px; }
  .chip.online .num { color: var(--online); }
  .chip.offline .num { color: var(--offline); }
  .chip.total .num { color: var(--accent); }
  #list { padding: 0 16px; display: flex; flex-direction: column; gap: 10px; }
  .row { background: var(--card); border-radius: 14px; padding: 12px 14px; display: flex; align-items: center; gap: 12px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .dot.online { background: var(--online); box-shadow: 0 0 6px var(--online); }
  .dot.offline { background: var(--offline); }
  .info { flex: 1; min-width: 0; }
  .name { font-weight: 600; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 12px; color: var(--muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .join { background: var(--accent); color: #1a1a1a; font-weight: 700; font-size: 12px; padding: 8px 12px; border-radius: 10px; text-decoration: none; flex-shrink: 0; }
  .empty { text-align: center; color: var(--muted); padding: 60px 20px; font-size: 14px; }
  #keyGate { position: fixed; inset: 0; background: var(--bg); display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 14px; padding: 24px; z-index: 10; }
  #keyGate input { background: var(--card); border: 1px solid #333; color: var(--text); padding: 12px 14px; border-radius: 10px; font-size: 15px; width: 100%; max-width: 280px; }
  #keyGate button { background: var(--accent); color: #1a1a1a; font-weight: 700; border: none; padding: 12px 20px; border-radius: 10px; font-size: 15px; cursor: pointer; }
  #keyGate p { color: var(--muted); font-size: 13px; text-align: center; max-width: 260px; }
  footer { text-align: center; color: #555; font-size: 11px; padding: 24px 16px 4px; }
  footer button { background: none; border: none; color: #555; text-decoration: underline; font-size: 11px; cursor: pointer; }
</style>
</head>
<body>

<div id="keyGate">
  <h1>DivineSouls <span style="color:#FF8C28">Dashboard</span></h1>
  <p>Enter your dashboard key (set as DASHBOARD_KEY on the bot) to view account status.</p>
  <input id="keyInput" type="password" placeholder="Dashboard key" autocomplete="off">
  <button id="keySubmit">Unlock</button>
</div>

<div id="app" style="display:none">
  <header>
    <h1>DivineSouls <span>Dashboard</span></h1>
    <button id="refreshBtn" title="Refresh">&#8635;</button>
  </header>
  <div class="summary">
    <div class="chip total"><div class="num" id="numTotal">-</div><div class="lbl">Total</div></div>
    <div class="chip online"><div class="num" id="numOnline">-</div><div class="lbl">Online</div></div>
    <div class="chip offline"><div class="num" id="numOffline">-</div><div class="lbl">Offline</div></div>
  </div>
  <div id="list"></div>
  <footer>
    Auto-refreshes every 15s &middot; <button id="resetKey">reset key</button>
  </footer>
</div>

<script>
const STORAGE_KEY = "ds_dashboard_key";
const gate = document.getElementById("keyGate");
const app = document.getElementById("app");
const list = document.getElementById("list");

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

function render(data) {
  document.getElementById("numTotal").textContent = data.total;
  document.getElementById("numOnline").textContent = data.online;
  document.getElementById("numOffline").textContent = data.offline;

  if (!data.accounts || data.accounts.length === 0) {
    list.innerHTML = `<div class="empty">No accounts reporting yet.</div>`;
    return;
  }

  list.innerHTML = data.accounts.map((a) => {
    const dotClass = a.online ? "online" : "offline";
    const statusText = a.online ? "online" : formatElapsed(a.lastSeenSecondsAgo) + " ago";
    const joinBtn = a.online && a.joinUrl ? `<a class="join" href="${a.joinUrl}">Join</a>` : "";
    return `
      <div class="row">
        <div class="dot ${dotClass}"></div>
        <div class="info">
          <div class="name">${escapeHtml(a.name)}</div>
          <div class="meta">${escapeHtml(a.game)} &middot; ${statusText}</div>
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
  gate.style.display = "none";
  app.style.display = "block";
  refresh();
}

function showGate() {
  gate.style.display = "flex";
  app.style.display = "none";
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

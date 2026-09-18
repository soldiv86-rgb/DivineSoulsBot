"""Discord bot: embeds, Status/Panel views, slash commands."""
import time

import discord
from discord import app_commands
from discord.ext import commands

from .config import (
    COLOR_PRIMARY,
    FOOTER_TEXT,
    PAGE_SIZE,
    MAX_EMBED_FIELDS,
    OFFLINE_TIMEOUT_MULTIPLIER,
    DASHBOARD_CHANNEL_ID,
    PUBLIC_BASE_URL,
)
from . import state
from .persistence import save_accounts, load_ui_settings, save_ui_settings
from .roblox_script import build_roblox_script

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
_panel_view_registered = False


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


def is_stale(data, now):
    """Online by timeout rule but last report is older than one interval (possible lag)."""
    if not is_account_online(data, now):
        return False
    interval = data.get("intervalSeconds", 300)
    return (now - data.get("lastSeen", 0)) > interval


def display_name(key, data):
    return data.get("playerName") or key


def sorted_accounts():
    ui = load_ui_settings()
    pinned = set(ui.get("pinned") or [])

    def sort_key(kv):
        key, data = kv
        name = display_name(key, data).lower()
        return (0 if key in pinned else 1, name)

    return sorted(state.accounts.items(), key=sort_key)


def styled_embed(title, color=COLOR_PRIMARY, description=None):
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    if description:
        embed.description = description
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def build_user_list():
    embed = styled_embed("👥 Accounts")
    if not state.accounts:
        embed.description = "No accounts reporting yet."
        return embed
    now = time.time()
    ui = load_ui_settings()
    pinned = set(ui.get("pinned") or [])
    lines = []
    for key, data in sorted_accounts():
        name = display_name(key, data)
        online = is_account_online(data, now)
        star = "★ " if key in pinned else ""
        status = "🟢 Online" if online else "🔴 Offline"
        game = data.get("gameName") or "Unknown"
        lines.append(f"{star}**{name}** — {status} · {game}")
    embed.description = "\n".join(lines)
    return embed


def build_summary_embed():
    now = time.time()
    total = len(state.accounts)
    online = sum(1 for _, data in state.accounts.items() if is_account_online(data, now))
    offline = total - online
    # Top games
    games = {}
    for _, data in state.accounts.items():
        g = data.get("gameName") or "Unknown"
        games[g] = games.get(g, 0) + 1
    top = sorted(games.items(), key=lambda x: (-x[1], x[0]))[:5]
    embed = styled_embed("📊 Summary")
    embed.add_field(name="Total", value=str(total), inline=True)
    embed.add_field(name="🟢 Online", value=str(online), inline=True)
    embed.add_field(name="🔴 Offline", value=str(offline), inline=True)
    if top:
        embed.add_field(
            name="Top games",
            value="\n".join(f"• **{g}** — {n}" for g, n in top),
            inline=False,
        )
    return embed


def build_panel_embed():
    return styled_embed(
        "🎮 DivineSoul Control Panel",
        description=(
            "Use the buttons below.\n"
            "• **Status** — live list with join links\n"
            "• **Script** — copy the Roblox reporter\n"
            "• **Refresh panel** — re-post this panel"
        ),
    )


class StatusView(discord.ui.View):
    def __init__(self, invoker_id):
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.page = 0
        self.rebuild()

    def max_page(self):
        total = len(state.accounts)
        return max(0, (total - 1) // PAGE_SIZE) if total else 0

    def get_page_entries(self):
        entries = sorted_accounts()
        if not entries:
            return []
        self.page = max(0, min(self.page, self.max_page()))
        start = self.page * PAGE_SIZE
        return entries[start:start + PAGE_SIZE]

    def build_embed(self):
        total = len(state.accounts)
        embed = styled_embed("📡 Account Status")
        if total == 0:
            embed.description = "No accounts reporting yet."
            return embed

        page_entries = self.get_page_entries()
        now = time.time()
        ui = load_ui_settings()
        pinned = set(ui.get("pinned") or [])

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
            if fields_used + 1 + len(group) > MAX_EMBED_FIELDS:
                remaining = sum(len(by_game[g]) for g in order[order.index(game):])
                embed.add_field(
                    name="⚠️ More on this page",
                    value=f"+{remaining} accounts not shown here.",
                    inline=False,
                )
                break

            embed.add_field(name=f"🎮 {game}", value="\u200b", inline=False)
            fields_used += 1
            for key, data in group:
                name = display_name(key, data)
                online = is_account_online(data, now)
                elapsed = now - data.get("lastSeen", now)
                star = "★ " if key in pinned else ""
                if online:
                    flag = " · ⚠️ stale" if is_stale(data, now) else ""
                    value = f"🟢 Online{flag} • {format_elapsed(elapsed)} ago"
                else:
                    value = f"🔴 Offline • {format_elapsed(elapsed)} ago"
                embed.add_field(
                    name=f"{star}👤 {name}",
                    value=value,
                    inline=True,
                )
                fields_used += 1

        embed.set_footer(
            text=f"{FOOTER_TEXT} • Page {self.page + 1} of {self.max_page() + 1} • {total} account(s)"
        )
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
            self.add_item(discord.ui.Button(label=f"Join {name}"[:80], url=url, row=row))
            col += 1
            if col >= 5:
                col, row = 0, row + 1
                if row > 4:
                    break


class RemoveConfirmView(discord.ui.View):
    def __init__(self, invoker_id: int, key: str):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id
        self.key = key

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This confirm isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm remove", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.key in state.accounts:
            name = display_name(self.key, state.accounts[self.key])
            del state.accounts[self.key]
            save_accounts()
            # also unpin
            ui = load_ui_settings()
            pinned = [p for p in (ui.get("pinned") or []) if p != self.key]
            if pinned != ui.get("pinned"):
                ui["pinned"] = pinned
                save_ui_settings(ui)
            await interaction.response.edit_message(
                content=f"Removed **{name}** (`{self.key}`) from the dashboard.",
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content=f"No account found for `{self.key}` (already gone?).",
                view=None,
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Remove cancelled.", view=None)
        self.stop()


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📡 Status", style=discord.ButtonStyle.success, custom_id="panel_status_v3")
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = StatusView(invoker_id=interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="👥 User List", style=discord.ButtonStyle.secondary, custom_id="panel_userlist_v2")
    async def userlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_user_list(), ephemeral=True)

    @discord.ui.button(label="📊 Summary", style=discord.ButtonStyle.primary, custom_id="panel_summary_v2")
    async def summary_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_summary_embed(), ephemeral=True)

    @discord.ui.button(label="📜 Script", style=discord.ButtonStyle.secondary, custom_id="panel_script_v1")
    async def script_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # The real REPORT_SECRET is deliberately NOT included here; it stays
        # behind the dashboard key (Script tab on the web dashboard).
        base = PUBLIC_BASE_URL or "https://YOUR-APP.onrender.com"
        script = build_roblox_script(base)
        dashboard_url = f"{PUBLIC_BASE_URL}/dashboard" if PUBLIC_BASE_URL else "your dashboard"
        note = (
            "Roblox **Server Script** (ServerScriptService). "
            "Enable **HttpService** in Game Settings → Security.\n"
            f"🔑 Your report secret isn't shown here. Open the **Script** tab on {dashboard_url} "
            "to copy the full script with the secret filled in.\n"
        )
        if not PUBLIC_BASE_URL:
            note += (
                "⚠️ Set env `PUBLIC_BASE_URL` (e.g. `https://your-app.onrender.com`) "
                "so the URL is correct.\n"
            )
        # Discord message limit 2000; code in block
        body = f"{note}```lua\n{script}\n```"
        if len(body) > 2000:
            body = note + "```lua\n" + script[:1500] + "\n-- truncated\n```"
        await interaction.response.send_message(body, ephemeral=True)

    @discord.ui.button(label="🔄 Refresh panel", style=discord.ButtonStyle.secondary, custom_id="panel_repost_v1")
    async def refresh_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=build_panel_embed(),
            view=PanelView(),
        )


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
    await interaction.response.send_message(embed=build_panel_embed(), view=PanelView())


@bot.tree.command(name="accounts", description="Quick summary: totals and top games.")
@in_dashboard_channel()
async def accounts_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_summary_embed(), ephemeral=True)


@bot.tree.command(name="pin", description="Pin an account so it sorts to the top (web + Discord).")
@app_commands.describe(key="Account to pin")
@in_dashboard_channel()
async def pin_command(interaction: discord.Interaction, key: str):
    if key not in state.accounts:
        await interaction.response.send_message(f"No account found for `{key}`.", ephemeral=True)
        return
    ui = load_ui_settings()
    pinned = list(ui.get("pinned") or [])
    if key not in pinned:
        pinned.append(key)
        ui["pinned"] = pinned
        save_ui_settings(ui)
    name = display_name(key, state.accounts[key])
    await interaction.response.send_message(f"Pinned **{name}**.", ephemeral=True)


@bot.tree.command(name="unpin", description="Unpin an account.")
@app_commands.describe(key="Account to unpin")
@in_dashboard_channel()
async def unpin_command(interaction: discord.Interaction, key: str):
    ui = load_ui_settings()
    pinned = [p for p in (ui.get("pinned") or []) if p != key]
    ui["pinned"] = pinned
    save_ui_settings(ui)
    await interaction.response.send_message(f"Unpinned `{key}`.", ephemeral=True)


@bot.tree.command(name="remove", description="Remove an account from the dashboard (asks for confirm).")
@app_commands.describe(key="The account to remove")
@in_dashboard_channel()
async def remove_command(interaction: discord.Interaction, key: str):
    if key not in state.accounts:
        await interaction.response.send_message(f"No account found for `{key}`.", ephemeral=True)
        return
    name = display_name(key, state.accounts[key])
    view = RemoveConfirmView(invoker_id=interaction.user.id, key=key)
    await interaction.response.send_message(
        f"Remove **{name}** (`{key}`) from the dashboard?",
        view=view,
        ephemeral=True,
    )


async def _account_key_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    choices = []
    for key, data in sorted_accounts():
        name = display_name(key, data)
        if current in name.lower() or current in key.lower():
            choices.append(app_commands.Choice(name=name[:100], value=key))
        if len(choices) >= 25:
            break
    return choices


@remove_command.autocomplete("key")
async def remove_autocomplete(interaction: discord.Interaction, current: str):
    return await _account_key_autocomplete(interaction, current)


@pin_command.autocomplete("key")
async def pin_autocomplete(interaction: discord.Interaction, current: str):
    return await _account_key_autocomplete(interaction, current)


@unpin_command.autocomplete("key")
async def unpin_autocomplete(interaction: discord.Interaction, current: str):
    return await _account_key_autocomplete(interaction, current)


@bot.event
async def setup_hook():
    await bot.tree.sync()


@bot.event
async def on_ready():
    global _panel_view_registered
    print(f"Logged in as {bot.user}", flush=True)
    if not _panel_view_registered:
        bot.add_view(PanelView())
        _panel_view_registered = True

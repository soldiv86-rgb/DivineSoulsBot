"""Environment and app constants."""
import os
from pathlib import Path

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DASHBOARD_CHANNEL_ID = int(os.environ["DASHBOARD_CHANNEL_ID"])
REPORT_SECRET = os.environ["REPORT_SECRET"]
DASHBOARD_KEY = os.environ["DASHBOARD_KEY"]

OFFLINE_TIMEOUT_MULTIPLIER = 2
WEB_SERVER_PORT = int(os.environ.get("PORT", 8080))
PAGE_SIZE = 10
MAX_EMBED_FIELDS = 24

DATA_FILE = Path(os.environ.get("DATA_FILE", "accounts.json"))
THEME_FILE = Path(os.environ.get("THEME_FILE", "theme.json"))

# Neon Postgres connection string. When set, accounts + theme are stored in
# the database (survives Render redeploys). When unset, local JSON files are used.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None

# The one-liner shown on the dashboard's Report Script tab and by the Discord
# Script button, for copying into an executor. Override with the
# REPORT_LOADSTRING env var if the GitHub link ever changes.
REPORT_LOADSTRING = (os.environ.get("REPORT_LOADSTRING") or "").strip() or (
    'loadstring(game:HttpGet("https://raw.githubusercontent.com/soldiv86-rgb/Report/refs/heads/main/Report"))()'
)

COLOR_PRIMARY = 0xFF8C28
COLOR_ONLINE = 0x57F287
FOOTER_TEXT = "DivineSoul Dashboard"

DEFAULT_THEME = {
    "accent": "#FF8C28",
    "mode": "dark",
}

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

STATUS_COLORS = {"online": "#57F287", "offline": "#ED4245"}

ICON_VERSION = "1"

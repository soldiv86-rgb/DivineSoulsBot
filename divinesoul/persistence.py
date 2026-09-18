"""Load/save accounts.json and theme.json."""
import json

from .config import DATA_FILE, THEME_FILE, DEFAULT_THEME, PALETTES
from . import state


def load_accounts():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r") as f:
                state.accounts = json.load(f)
            print(f"Loaded {len(state.accounts)} account(s) from {DATA_FILE}", flush=True)
        except Exception as e:
            print(f"Could not read {DATA_FILE} ({e}) - starting with an empty dashboard.", flush=True)
            state.accounts = {}
    else:
        state.accounts = {}


def save_accounts():
    try:
        tmp_path = DATA_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(state.accounts, f)
        tmp_path.replace(DATA_FILE)
    except Exception as e:
        print(f"Failed to save accounts to {DATA_FILE}: {e}", flush=True)


def load_theme():
    if THEME_FILE.exists():
        try:
            with open(THEME_FILE, "r") as f:
                saved = json.load(f)
            state.theme = {**DEFAULT_THEME, **{k: v for k, v in saved.items() if k in DEFAULT_THEME}}
            if state.theme.get("mode") not in PALETTES:
                state.theme["mode"] = DEFAULT_THEME["mode"]
            print(f"Loaded custom theme from {THEME_FILE}", flush=True)
        except Exception as e:
            print(f"Could not read {THEME_FILE} ({e}) - using default theme.", flush=True)
            state.theme = dict(DEFAULT_THEME)
    else:
        state.theme = dict(DEFAULT_THEME)


def save_theme():
    try:
        tmp_path = THEME_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(state.theme, f)
        tmp_path.replace(THEME_FILE)
    except Exception as e:
        print(f"Failed to save theme to {THEME_FILE}: {e}", flush=True)

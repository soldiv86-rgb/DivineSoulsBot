"""Load/save accounts + theme + UI settings.

Uses Neon Postgres when DATABASE_URL is set (recommended, survives
Render redeploys). Falls back to local JSON files if DATABASE_URL is
missing, so local testing still works without a database.

Threading model
---------------
* The ``load_*`` / ``init_*`` functions are blocking and are only meant to be
  called once at startup, before the bot starts serving requests.
* The ``save_*`` functions never block the asyncio event loop. They queue a
  background write (run in a worker thread) and return immediately. Several
  saves in quick succession are coalesced into one write of the latest data.
* ``load_ui_settings()`` reads an in-memory copy, so it is safe to call from
  request handlers and Discord interactions (no database round trip).
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .config import (
    DATA_FILE,
    THEME_FILE,
    DEFAULT_THEME,
    PALETTES,
    DATABASE_URL,
)
from . import state

UI_SETTINGS_FILE = Path("ui_settings.json")

_pg = None
_schema_ready = False
_schema_lock = threading.Lock()


def _using_postgres() -> bool:
    return bool(DATABASE_URL)


def _psycopg():
    global _pg
    if _pg is None:
        try:
            import psycopg
            from psycopg.types.json import Jsonb
            _pg = (psycopg, Jsonb)
        except ImportError as e:
            raise RuntimeError(
                "DATABASE_URL is set but psycopg is not installed. "
                "Add psycopg[binary] to requirements.txt."
            ) from e
    return _pg


def _connect():
    psycopg, _ = _psycopg()
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    # Saves run in worker threads, so guard against two of them racing to
    # create the table at the same time.
    with _schema_lock:
        if _schema_ready:
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_store (
                name TEXT PRIMARY KEY,
                data JSONB NOT NULL
            )
            """
        )
        conn.commit()
        _schema_ready = True


def _pg_get(name: str) -> Optional[Any]:
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT data FROM app_store WHERE name = %s",
            (name,),
        ).fetchone()
        return row[0] if row else None


def _pg_set(name: str, data: Any) -> None:
    _, Jsonb = _psycopg()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO app_store (name, data)
            VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data
            """,
            (name, Jsonb(data)),
        )
        conn.commit()


# ---- Blocking writers (run in a worker thread, or at startup) ----

def _write_json_atomic(path: Path, data: Any) -> None:
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    tmp_path.replace(path)


def _write_accounts(data: dict) -> None:
    if _using_postgres():
        try:
            _pg_set("accounts", data)
        except Exception as e:
            print(f"Failed to save accounts to Neon: {e}", flush=True)
        return

    try:
        _write_json_atomic(DATA_FILE, data)
    except Exception as e:
        print(f"Failed to save accounts to {DATA_FILE}: {e}", flush=True)


def _write_theme(data: dict) -> None:
    if _using_postgres():
        try:
            _pg_set("theme", data)
        except Exception as e:
            print(f"Failed to save theme to Neon: {e}", flush=True)
        return

    try:
        _write_json_atomic(THEME_FILE, data)
    except Exception as e:
        print(f"Failed to save theme to {THEME_FILE}: {e}", flush=True)


def _write_ui_settings(data: dict) -> None:
    try:
        if _using_postgres():
            _pg_set("ui_settings", data)
            return
    except Exception as e:
        print(f"save_ui_settings: {e}", flush=True)
    try:
        _write_json_atomic(UI_SETTINGS_FILE, data)
    except Exception as e:
        print(f"save_ui_settings json: {e}", flush=True)


# ---- Background saver ----

class _BackgroundSaver:
    """Runs a blocking write in a worker thread without stalling the event loop.

    ``request()`` is cheap and non-blocking. If a write is already in flight,
    the request just marks the data dirty and the running task writes the
    newest snapshot again when it finishes, so bursts of saves collapse into
    at most one extra write.
    """

    def __init__(self, snapshot: Callable[[], Any], write: Callable[[Any], None]):
        self._snapshot = snapshot
        self._write = write
        self._dirty = False
        self._task: Optional[asyncio.Task] = None  # strong ref keeps it alive

    def request(self) -> None:
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (scripts/tests): just write synchronously.
            self._dirty = False
            self._write(self._snapshot())
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        while self._dirty:
            self._dirty = False
            # Snapshot on the event-loop thread so the worker thread never
            # iterates a dict that request handlers are still mutating.
            snap = self._snapshot()
            try:
                await asyncio.to_thread(self._write, snap)
            except Exception as e:  # the writers already catch; belt and braces
                print(f"background save failed: {e}", flush=True)


_accounts_saver = _BackgroundSaver(lambda: copy.deepcopy(state.accounts), _write_accounts)
_theme_saver = _BackgroundSaver(lambda: dict(state.theme), _write_theme)
_ui_saver = _BackgroundSaver(lambda: copy.deepcopy(state.ui_settings), _write_ui_settings)


# ---- Accounts ----

def load_accounts() -> None:
    if _using_postgres():
        try:
            data = _pg_get("accounts")
            if isinstance(data, dict):
                state.accounts = data
                print(f"Loaded {len(state.accounts)} account(s) from Neon Postgres", flush=True)
            else:
                state.accounts = {}
                if DATA_FILE.exists():
                    try:
                        with open(DATA_FILE, "r") as f:
                            state.accounts = json.load(f)
                        if state.accounts:
                            _write_accounts(state.accounts)
                            print(
                                f"Migrated {len(state.accounts)} account(s) "
                                f"from {DATA_FILE} -> Neon",
                                flush=True,
                            )
                    except Exception as e:
                        print(f"JSON migrate skipped ({e})", flush=True)
                        state.accounts = {}
                else:
                    print("No accounts in Neon yet - starting empty", flush=True)
        except Exception as e:
            print(f"Neon load_accounts failed ({e}) - starting empty", flush=True)
            state.accounts = {}
        return

    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r") as f:
                state.accounts = json.load(f)
            print(f"Loaded {len(state.accounts)} account(s) from {DATA_FILE}", flush=True)
        except Exception as e:
            print(f"Could not read {DATA_FILE} ({e}) - starting empty", flush=True)
            state.accounts = {}
    else:
        state.accounts = {}


def save_accounts() -> None:
    """Queue a background write of state.accounts (does not block)."""
    _accounts_saver.request()


# ---- Theme ----

def load_theme() -> None:
    if _using_postgres():
        try:
            data = _pg_get("theme")
            if isinstance(data, dict):
                state.theme = {
                    **DEFAULT_THEME,
                    **{k: v for k, v in data.items() if k in DEFAULT_THEME},
                }
                if state.theme.get("mode") not in PALETTES:
                    state.theme["mode"] = DEFAULT_THEME["mode"]
                print("Loaded theme from Neon Postgres", flush=True)
            else:
                state.theme = dict(DEFAULT_THEME)
                if THEME_FILE.exists():
                    try:
                        with open(THEME_FILE, "r") as f:
                            saved = json.load(f)
                        state.theme = {
                            **DEFAULT_THEME,
                            **{k: v for k, v in saved.items() if k in DEFAULT_THEME},
                        }
                        _write_theme(state.theme)
                        print(f"Migrated theme from {THEME_FILE} -> Neon", flush=True)
                    except Exception as e:
                        print(f"Theme JSON migrate skipped ({e})", flush=True)
                        state.theme = dict(DEFAULT_THEME)
                else:
                    print("No theme in Neon - using default", flush=True)
        except Exception as e:
            print(f"Neon load_theme failed ({e}) - using default", flush=True)
            state.theme = dict(DEFAULT_THEME)
        return

    if THEME_FILE.exists():
        try:
            with open(THEME_FILE, "r") as f:
                saved = json.load(f)
            state.theme = {
                **DEFAULT_THEME,
                **{k: v for k, v in saved.items() if k in DEFAULT_THEME},
            }
            if state.theme.get("mode") not in PALETTES:
                state.theme["mode"] = DEFAULT_THEME["mode"]
            print(f"Loaded theme from {THEME_FILE}", flush=True)
        except Exception as e:
            print(f"Could not read {THEME_FILE} ({e}) - default theme", flush=True)
            state.theme = dict(DEFAULT_THEME)
    else:
        state.theme = dict(DEFAULT_THEME)


def save_theme() -> None:
    """Queue a background write of state.theme (does not block)."""
    _theme_saver.request()


# ---- UI settings (pins, sort, density, …) shared by web + Discord ----
DEFAULT_UI_SETTINGS = {
    "pinned": [],
    "sort": "online_first",
    "density": "comfortable",
    "refreshSeconds": 15,
    "quietHours": {"enabled": False, "start": 23, "end": 7},
}


def _normalize_ui(data: Any) -> dict:
    """Merge saved settings over the defaults, returning a fresh deep copy."""
    data = copy.deepcopy(data) if isinstance(data, dict) else {}
    out = {**copy.deepcopy(DEFAULT_UI_SETTINGS), **data}
    quiet = data.get("quietHours")
    out["quietHours"] = {
        **DEFAULT_UI_SETTINGS["quietHours"],
        **(quiet if isinstance(quiet, dict) else {}),
    }
    return out


def init_ui_settings() -> None:
    """Load UI settings from storage into memory. Call once at startup."""
    data = None
    try:
        if _using_postgres():
            data = _pg_get("ui_settings")
        elif UI_SETTINGS_FILE.exists():
            data = json.loads(UI_SETTINGS_FILE.read_text())
    except Exception as e:
        print(f"init_ui_settings: {e}", flush=True)
    state.ui_settings = _normalize_ui(data)
    print("Loaded UI settings", flush=True)


def load_ui_settings() -> dict:
    """Current UI settings from memory (no I/O). Returns a copy, safe to mutate."""
    return _normalize_ui(state.ui_settings)


def save_ui_settings(settings: dict) -> None:
    """Update the in-memory settings now and queue a background write."""
    state.ui_settings = _normalize_ui(settings)
    _ui_saver.request()

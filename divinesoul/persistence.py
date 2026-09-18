"""Load/save accounts + theme.

Uses Neon Postgres when DATABASE_URL is set (recommended, survives
Render redeploys). Falls back to local JSON files if DATABASE_URL is
missing, so local testing still works without a database.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .config import (
    DATA_FILE,
    THEME_FILE,
    DEFAULT_THEME,
    PALETTES,
    DATABASE_URL,
)
from . import state

_pg = None
_schema_ready = False


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
                            save_accounts()
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
    if _using_postgres():
        try:
            _pg_set("accounts", state.accounts)
        except Exception as e:
            print(f"Failed to save accounts to Neon: {e}", flush=True)
        return

    try:
        tmp_path = DATA_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(state.accounts, f)
        tmp_path.replace(DATA_FILE)
    except Exception as e:
        print(f"Failed to save accounts to {DATA_FILE}: {e}", flush=True)


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
                        save_theme()
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
    if _using_postgres():
        try:
            _pg_set("theme", state.theme)
        except Exception as e:
            print(f"Failed to save theme to Neon: {e}", flush=True)
        return

    try:
        tmp_path = THEME_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(state.theme, f)
        tmp_path.replace(THEME_FILE)
    except Exception as e:
        print(f"Failed to save theme to {THEME_FILE}: {e}", flush=True)

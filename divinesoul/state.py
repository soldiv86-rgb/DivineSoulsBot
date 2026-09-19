"""Shared runtime state (accounts + theme + UI settings dicts)."""
from .config import DEFAULT_THEME

accounts = {}  # key -> account report dict
theme = dict(DEFAULT_THEME)
ui_settings = {}  # cached UI settings; filled by persistence.init_ui_settings()
apks = {}  # id -> {"name","platform","filename","size","uploaded_at"} for dashboard-uploaded builds

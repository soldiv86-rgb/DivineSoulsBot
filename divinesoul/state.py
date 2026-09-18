"""Shared runtime state (accounts + theme dicts)."""
from .config import DEFAULT_THEME

accounts = {}  # key -> account report dict
theme = dict(DEFAULT_THEME)

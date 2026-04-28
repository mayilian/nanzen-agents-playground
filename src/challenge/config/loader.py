"""Centralised YAML configuration loader.

Configuration values that may need tuning *without* code review live in
the project's ``config/`` directory. This module is the single point of
access; modules consume parsed dicts rather than reading YAML directly.

Why this matters:
- A typo correction or threshold change should not be a Python diff.
- Per-customer overrides should be possible without forking the code.
- The full set of "what's tunable" is greppable in one place.

Caching:
- Files are read once per process; mtime-invalidation is intentionally
  not implemented (configs are loaded at pipeline start, not per-request).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "config"

_cache: dict[Path, dict] = {}
_cache_lock = threading.Lock()


def load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file. Returns ``{}`` for an empty file.

    Raises:
        FileNotFoundError: if the path does not exist.
        yaml.YAMLError: on malformed YAML.
    """
    path = Path(path)
    with _cache_lock:
        cached = _cache.get(path)
        if cached is not None:
            return cached

    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    with _cache_lock:
        _cache[path] = data
    return data


def config_dir() -> Path:
    """Project-relative path to the config/ directory."""
    return CONFIG_DIR_DEFAULT


def reset_cache() -> None:
    """Clear the in-memory cache. Used by tests."""
    with _cache_lock:
        _cache.clear()

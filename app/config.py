"""Configuration loading (config.yml).

Path: ``TM_CONFIG`` environment variable (set by the systemd unit),
else ``config.yml`` at the root, failing that ``config.yml.example`` for a
first try without configuring anything.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def config_path() -> Path | None:
    env = os.environ.get("TM_CONFIG")
    if env:
        path = Path(env)
        if not path.is_file():
            raise FileNotFoundError(f"TM_CONFIG={env} : fichier introuvable")
        return path
    for path in (ROOT / "config.yml", ROOT / "config.yml.example"):
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    path = config_path()
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _section(name: str) -> dict[str, Any]:
    return load_config().get(name) or {}


def site_config() -> dict[str, Any]:
    return _section("site")


def server_config() -> dict[str, Any]:
    return _section("server")


def club_config() -> dict[str, Any]:
    """Radio club activating the callsigns (name, callsign, city, website)."""
    return _section("club")


def qrz_config() -> dict[str, Any]:
    return _section("qrz")


def activation_config() -> dict[str, Any]:
    return _section("activation")

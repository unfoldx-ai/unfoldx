"""Settings with fixed precedence: flags > env > project file > user file > defaults."""

from __future__ import annotations

import os
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import UnfoldxError

# The public API opens on the development site first.
DEFAULT_API_BASE = "https://dev.unfoldx.ai/api"
PROJECT_CONFIG_NAME = ".unfoldx.toml"


def config_dir() -> Path:
    return Path(os.environ.get("UNFOLDX_CONFIG_DIR") or Path.home() / ".config" / "unfoldx")


@dataclass
class Settings:
    api_base: str = DEFAULT_API_BASE
    timeout_sec: float = 30.0
    wait_timeout_sec: float = 3600.0
    poll_interval_sec: float = 5.0


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise UnfoldxError("usage", f"Invalid TOML configuration: {path}", code="invalid_config") from None


def load_settings(api_base_flag: str | None = None, timeout_flag: float | None = None) -> Settings:
    settings = Settings()
    # Low to high priority; later sources overwrite earlier ones.
    for source in (_read_toml(config_dir() / "config.toml"), _read_toml(Path.cwd() / PROJECT_CONFIG_NAME)):
        if "api_base" in source:
            settings.api_base = str(source["api_base"])
        for key in ("timeout_sec", "wait_timeout_sec", "poll_interval_sec"):
            if key in source:
                try:
                    value = float(source[key])
                except (TypeError, ValueError):
                    raise UnfoldxError("usage", f"{key} must be a positive number.", code="invalid_config") from None
                if not math.isfinite(value) or value <= 0:
                    raise UnfoldxError("usage", f"{key} must be a positive finite number.", code="invalid_config")
                setattr(settings, key, value)
    settings.api_base = os.environ.get("UNFOLDX_API_BASE") or settings.api_base
    if api_base_flag:
        settings.api_base = api_base_flag
    if timeout_flag is not None:
        settings.timeout_sec = timeout_flag
    settings.api_base = settings.api_base.rstrip("/")
    return settings

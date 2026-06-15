"""Local connection profile for cli-anything-alexa.

Stores the Amazon account email + region and the config dir holding the
alexapy cookie pickle. Lives at ~/.config/cli-anything-alexa/config.json
(mode 0600). The cookie pickle itself sits alongside it as
alexa_media.<email>.pickle. Never commit either.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = Path.home() / ".config" / "cli-anything-alexa"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    "email": None,
    # Amazon account region host (drives the alexa.<region> base url).
    "url": "amazon.co.uk",
}


def load_config(path: Optional[Path] = None) -> dict:
    p = path or DEFAULT_CONFIG_PATH
    out = dict(DEFAULTS)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                out.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    for k in list(out.keys()):
        env = "CLI_ALEXA_" + k.upper()
        if env in os.environ:
            out[k] = os.environ[env]
    return out


def save_config(cfg: dict, path: Optional[Path] = None) -> Path:
    p = path or DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in cfg.items() if k in DEFAULTS}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2, sort_keys=True)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def merge_cli_overrides(cfg: dict, **kwargs) -> dict:
    out = dict(cfg)
    for k, v in kwargs.items():
        if v is not None:
            out[k] = v
    return out

"""Config loading — YAML files plus environment-variable secrets.

Secrets never live in YAML. The YAML files reference an env-var *name*
(e.g. ``token_env: APIFY_TOKEN``); this module resolves the actual value from
the environment at runtime. Locally, a ``.env`` file is loaded if present.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

# Load a local .env if python-dotenv is installed (optional; CI uses real env).
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience, not required
    pass

# jobRadar/  (this file lives at jobRadar/common/config.py)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@lru_cache(maxsize=None)
def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


def pipelines() -> dict[str, Any]:
    return _load_yaml("pipelines.yaml")


def scoring() -> dict[str, Any]:
    return _load_yaml("scoring.yaml")


@lru_cache(maxsize=None)
def watchlist() -> dict[str, Any]:
    """Optional company watchlist for ATS scanning. Absent file -> empty."""
    path = CONFIG_DIR / "watchlist.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=None)
def outreach() -> dict[str, Any]:
    """Optional cold-outreach config. Absent file -> empty (feature off)."""
    path = CONFIG_DIR / "outreach.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def env(name: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """Read a secret from the environment.

    ``name`` may be None (some optional sources have no configured env key),
    in which case ``default`` is returned.

    The value is stripped of surrounding whitespace: GitHub Actions secrets are
    routinely stored with a trailing newline, and an API key with a ``\\n`` makes
    ``requests`` reject the ``Authorization``/``X-API-KEY`` header outright
    ("Invalid ... return character(s) in header value"). No key/token/id we read
    has meaningful leading or trailing whitespace, so stripping is always safe.
    """
    if not name:
        return default
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val or default


def require_env(name: str) -> str:
    val = os.environ.get(name)
    val = val.strip() if val else val
    if not val:
        raise RuntimeError(f"Required secret ${name} is not set")
    return val

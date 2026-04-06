"""Client configuration backed by a TOML file.

Reads from ``$XDG_CONFIG_HOME/biorxiv-mcp/config.toml`` (defaults to
``~/.config/biorxiv-mcp/config.toml``).  Environment variables always
take precedence over the config file.

The config file is intentionally minimal::

    [client]
    url = "https://biorxiv.example.com"
    api_key = "tok_..."
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

_APP = "biorxiv-mcp"


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / _APP


def config_path() -> Path:
    """Return the path to the config file (may not exist yet)."""
    return _config_dir() / "config.toml"


def _read_table() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f).get("client", {})


def get_url() -> str:
    """Return the API URL: env var > config file > localhost default."""
    env = os.environ.get("BIORXIV_API_URL")
    if env:
        return env
    return _read_table().get("url", "http://localhost:8000")


def get_api_key() -> str | None:
    """Return the API key: env var > config file > None."""
    env = os.environ.get("BIORXIV_API_KEY")
    if env:
        return env
    return _read_table().get("api_key") or None


def save(url: str, api_key: str | None = None) -> Path:
    """Write client config to disk. Returns the config file path."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[client]", f'url = "{url}"']
    if api_key:
        lines.append(f'api_key = "{api_key}"')
    p.write_text("\n".join(lines) + "\n")
    return p

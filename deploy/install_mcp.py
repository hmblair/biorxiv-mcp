#!/usr/bin/env python3
"""Register/unregister the biorxiv-mcp HTTP endpoint with agent tools.

Targets Claude Code (via the ``claude`` CLI), Claude Desktop (via
``claude_desktop_config.json``), and OpenCode (via ``opencode.json``).

Usage:
    install_mcp.py install [--url http://localhost:8000/mcp] [--name biorxiv-mcp]
    install_mcp.py uninstall [--name biorxiv-mcp]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HOME = Path.home()

if sys.platform == "darwin":
    CLAUDE_DESKTOP = HOME / "Library/Application Support/Claude/claude_desktop_config.json"
elif sys.platform == "win32":
    CLAUDE_DESKTOP = HOME / "AppData/Roaming/Claude/claude_desktop_config.json"
else:
    CLAUDE_DESKTOP = HOME / ".config/Claude/claude_desktop_config.json"

OPENCODE = HOME / ".config/opencode/opencode.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# -- Claude Code --------------------------------------------------------------

def install_claude_code(name: str, url: str) -> None:
    subprocess.run(["claude", "mcp", "remove", "--scope", "user", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    try:
        subprocess.run(
            ["claude", "mcp", "add", "--transport", "http", "--scope", "user", name, url],
            check=True,
        )
        print(f"  Added {name} to Claude Code")
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(f"  claude CLI unavailable; add manually:")
        print(f"    claude mcp add --transport http --scope user {name} {url}")


def uninstall_claude_code(name: str) -> None:
    subprocess.run(["claude", "mcp", "remove", "--scope", "user", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print(f"  Removed {name} from Claude Code (if present)")


# -- Claude Desktop -----------------------------------------------------------

def install_claude_desktop(name: str, url: str) -> None:
    if not CLAUDE_DESKTOP.parent.exists():
        print(f"  Claude Desktop config dir not found; skipping ({CLAUDE_DESKTOP.parent})")
        return
    config = _read_json(CLAUDE_DESKTOP)
    config.setdefault("mcpServers", {})[name] = {"url": url}
    _write_json(CLAUDE_DESKTOP, config)
    print(f"  Added {name} to {CLAUDE_DESKTOP}")


def uninstall_claude_desktop(name: str) -> None:
    config = _read_json(CLAUDE_DESKTOP)
    if config.get("mcpServers", {}).pop(name, None) is not None:
        _write_json(CLAUDE_DESKTOP, config)
        print(f"  Removed {name} from {CLAUDE_DESKTOP}")


# -- OpenCode -----------------------------------------------------------------

def install_opencode(name: str, url: str) -> None:
    config = _read_json(OPENCODE) or {"$schema": "https://opencode.ai/config.json"}
    config.setdefault("mcp", {})[name] = {"type": "remote", "url": url, "enabled": True}
    _write_json(OPENCODE, config)
    print(f"  Added {name} to {OPENCODE}")


def uninstall_opencode(name: str) -> None:
    config = _read_json(OPENCODE)
    if config.get("mcp", {}).pop(name, None) is not None:
        _write_json(OPENCODE, config)
        print(f"  Removed {name} from {OPENCODE}")


# -- CLI ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--name", default="biorxiv-mcp")
    parser.add_argument("--url", default="http://localhost:8000/mcp")
    args = parser.parse_args()

    print()
    if args.action == "install":
        install_claude_code(args.name, args.url)
        install_claude_desktop(args.name, args.url)
        install_opencode(args.name, args.url)
    else:
        uninstall_claude_code(args.name)
        uninstall_claude_desktop(args.name)
        uninstall_opencode(args.name)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

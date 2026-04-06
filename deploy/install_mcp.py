#!/usr/bin/env python3
"""Register/unregister the biorxiv-mcp stdio shim with agent tools.

The shim is a local stdio process that proxies MCP tool calls to a
remote REST API. Connection settings are read from the config file
(~/.config/biorxiv-mcp/config.toml) and can be overridden with flags.

Usage:
    install_mcp.py install [--url ...] [--key ...] [--name ...]
    install_mcp.py uninstall [--name biorxiv-mcp]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Allow importing from the package tree.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from biorxiv_mcp.client.config import get_api_key, get_url
from biorxiv_mcp.client.config import save as save_config

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


def _find_shim() -> str:
    """Find the biorxiv-mcp entry point on PATH or in the local venv."""
    venv = Path(__file__).resolve().parent.parent / ".venv/bin/biorxiv-mcp"
    if venv.exists():
        return str(venv)
    found = shutil.which("biorxiv-mcp")
    if found:
        return found
    return "biorxiv-mcp"  # hope it's on PATH at runtime


def _env_dict(url: str, key: str | None) -> dict[str, str]:
    env = {"BIORXIV_API_URL": url}
    if key:
        env["BIORXIV_API_KEY"] = key
    return env


def _preflight(url: str, key: str | None) -> bool:
    """Quick check that the server is reachable."""
    import urllib.error
    import urllib.request

    health_url = url.rstrip("/") + "/health"
    req = urllib.request.Request(health_url)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            count = data.get("paper_count", "?")
            print(f"  Server healthy: {count} papers, last sync {data.get('last_sync', '?')}")
            return True
    except urllib.error.HTTPError as e:
        print(f"  WARNING: /health returned HTTP {e.code}")
        return False
    except Exception as e:
        print(f"  WARNING: cannot reach {health_url}: {e}")
        return False


# -- Claude Code --------------------------------------------------------------

def install_claude_code(name: str, shim: str, env: dict) -> None:
    subprocess.run(["claude", "mcp", "remove", "--scope", "user", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    cmd = ["claude", "mcp", "add", "--scope", "user", name]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--", shim]
    try:
        subprocess.run(cmd, check=True)
        print(f"  Added {name} to Claude Code")
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("  claude CLI unavailable; add manually with:")
        print("    " + " ".join(cmd))


def uninstall_claude_code(name: str) -> None:
    subprocess.run(["claude", "mcp", "remove", "--scope", "user", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print(f"  Removed {name} from Claude Code (if present)")


# -- Claude Desktop -----------------------------------------------------------

def install_claude_desktop(name: str, shim: str, env: dict) -> None:
    if not CLAUDE_DESKTOP.parent.exists():
        print(f"  Claude Desktop config dir not found; skipping ({CLAUDE_DESKTOP.parent})")
        return
    config = _read_json(CLAUDE_DESKTOP)
    config.setdefault("mcpServers", {})[name] = {
        "command": shim,
        "args": [],
        "env": env,
    }
    _write_json(CLAUDE_DESKTOP, config)
    print(f"  Added {name} to {CLAUDE_DESKTOP}")


def uninstall_claude_desktop(name: str) -> None:
    config = _read_json(CLAUDE_DESKTOP)
    if config.get("mcpServers", {}).pop(name, None) is not None:
        _write_json(CLAUDE_DESKTOP, config)
        print(f"  Removed {name} from {CLAUDE_DESKTOP}")


# -- OpenCode -----------------------------------------------------------------

def install_opencode(name: str, shim: str, env: dict) -> None:
    config = _read_json(OPENCODE) or {"$schema": "https://opencode.ai/config.json"}
    config.setdefault("mcp", {})[name] = {
        "type": "local",
        "command": [shim],
        "env": env,
        "enabled": True,
    }
    _write_json(OPENCODE, config)
    print(f"  Added {name} to {OPENCODE}")


def uninstall_opencode(name: str) -> None:
    config = _read_json(OPENCODE)
    if config.get("mcp", {}).pop(name, None) is not None:
        _write_json(OPENCODE, config)
        print(f"  Removed {name} from {OPENCODE}")


# -- CLI ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--name", default="biorxiv-mcp")
    parser.add_argument("--url", default=None,
                        help="API base URL (default: $BIORXIV_API_URL or http://localhost:8000)")
    parser.add_argument("--key", default=None,
                        help="API bearer token (default: $BIORXIV_API_KEY)")
    args = parser.parse_args()

    print()
    if args.action == "install":
        url = args.url or get_url()
        key = args.key or get_api_key() or ""
        shim = _find_shim()

        print(f"  Endpoint: {url}")
        print(f"  Auth:     {'configured' if key else 'none (localhost mode)'}")
        print(f"  Shim:     {shim}")
        print()

        _preflight(url, key or None)
        print()

        env = _env_dict(url, key or None)
        cfg = save_config(url, key or None)
        print(f"  Config saved to {cfg}")
        print()

        install_claude_code(args.name, shim, env)
        install_claude_desktop(args.name, shim, env)
        install_opencode(args.name, shim, env)
    else:
        uninstall_claude_code(args.name)
        uninstall_claude_desktop(args.name)
        uninstall_opencode(args.name)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Local settings that must never end up in git.

Everything machine- or user-specific (where the themes live, where the
credentials file goes, the Anthropic API key) is read from
`config.local.json` next to the program. That file is gitignored; the
committed `config.example.json` shows its shape.

Missing file or missing keys fall back to DEFAULTS, so the program also
runs with no config at all.
"""

import json
import os
import sys

CONFIG_FILENAME = "config.local.json"

DEFAULTS = {
    # Folder with per-site themes (<domain>.zip or <domain>/). Empty means
    # "not set yet": the GUI asks for it with Browse... and remembers it.
    "themes_dir": "",
    # Where WP admin passwords are appended. Relative paths are resolved
    # against the program folder.
    "credentials_file": "credentials.txt",
    # AI failure analysis (see wp_deploy_ai.py).
    "ai_enabled": True,
    # "gemini" (has a free tier) or "claude" (paid).
    "ai_provider": "gemini",
    "gemini_model": "gemini-3.1-flash-lite",
    "ai_model": "claude-opus-5-5",
    # Prefer the GEMINI_API_KEY / ANTHROPIC_API_KEY environment variables;
    # these are a fallback for launching the .exe by double-click.
    "gemini_api_key": "",
    "anthropic_api_key": "",
}


def app_dir() -> str:
    """Directory the program lives in (handles a frozen/exe build too)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(app_dir(), CONFIG_FILENAME)


def load_config() -> dict:
    """DEFAULTS overlaid with config.local.json. A broken file is reported,
    not fatal: the program still starts on defaults."""
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        except (OSError, json.JSONDecodeError) as e:
            cfg["_load_error"] = f"{CONFIG_FILENAME}: {e}"
    return cfg


def save_config(cfg: dict) -> None:
    """Write the known keys back to config.local.json."""
    data = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def resolve_path(path: str) -> str:
    """Relative paths in the config are relative to the program folder."""
    path = os.path.expandvars(os.path.expanduser(path.strip()))
    return path if os.path.isabs(path) else os.path.join(app_dir(), path)

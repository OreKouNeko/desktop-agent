# -*- coding: utf-8 -*-
"""Single source of truth for configuration.

Nothing here is tied to one machine: the data directory, endpoint, model and
credentials come from environment variables or a local config file that is
never committed to the repository.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .secret import protect, unprotect

APP_NAME = 'desktop-agent'

# Override with DESKTOP_AGENT_HOME to keep runs/macros somewhere else.
HOME = Path(os.environ.get('DESKTOP_AGENT_HOME') or (Path.home() / '.desktop-agent'))
RUNS_DIR = str(HOME / 'runs')
MACROS_DIR = str(HOME / 'macros')
CONFIG_FILE = HOME / 'config.json'
KEY_FILE = HOME / 'key.bin'

DEFAULTS = {
    'base_url': 'https://api.deepseek.com',
    'model': 'deepseek-flash',
    'max_steps': 100,
}


def ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    Path(RUNS_DIR).mkdir(parents=True, exist_ok=True)
    Path(MACROS_DIR).mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        if CONFIG_FILE.exists():
            data.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass
    return data


def save_settings(**kw) -> dict:
    ensure_home()
    data = load_settings()
    data.update({k: v for k, v in kw.items() if v is not None})
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def load_api_key() -> str:
    """Environment first, then the DPAPI-encrypted file. Never in the repo."""
    key = os.environ.get('DESKTOP_AGENT_API_KEY', '').strip()
    if key:
        return key
    try:
        if KEY_FILE.exists():
            return unprotect(KEY_FILE.read_bytes()).strip()
    except Exception:
        pass
    return ''


def save_api_key(key: str) -> None:
    ensure_home()
    KEY_FILE.write_bytes(protect(key))


def has_api_key() -> bool:
    return bool(load_api_key())

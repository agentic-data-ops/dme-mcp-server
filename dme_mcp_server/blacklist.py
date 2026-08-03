"""Blacklist control mechanism (mirrors ``load_blacklist`` / ``_accepts_risk`` / ``_check_risk`` in ``pydme/cli.py``).

Load priority:
1. User-defined ``~/.config/pydme/blacklist.json`` (authoritative)
2. ``pydme.config.blacklist.json`` read via ``importlib.resources`` (if shipped in the SDK wheel)
3. ``config/blacklist.json`` resolved from ``pydme.__file__`` (source / local install)

If all sources fail: print a warning and return an empty blacklist (does not block MCP Server startup, same behavior as cli.py).
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

_USER_PATH = Path.home() / '.config' / 'pydme' / 'blacklist.json'


def load_blacklist() -> Dict[str, list]:
    """Load the risk-operation blacklist -> ``{topic: [action_key, ...]}``."""
    # 1) User override (authoritative)
    if _USER_PATH.exists():
        try:
            return json.loads(_USER_PATH.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            print(f'warning: failed to read blacklist ({e}); skipping risk checks', file=sys.stderr)
            return {}

    # 2) SDK built-in default (importlib.resources)
    data = _read_sdk_default()
    if data is not None:
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            print(f'warning: failed to parse SDK default blacklist ({e}); skipping risk checks', file=sys.stderr)
            return {}

    print('warning: blacklist.json not found (SDK does not ship it); skipping risk checks', file=sys.stderr)
    return {}


def _read_sdk_default() -> str | None:
    """Read the default blacklist content from dme-python-sdk, with multi-source fallback."""
    # 2a) importlib.resources (when the SDK wheel bundles it)
    try:
        from importlib.resources import files as res_files
        return res_files('pydme.config').joinpath('blacklist.json').read_text(encoding='utf-8')
    except (ImportError, ModuleNotFoundError, FileNotFoundError, OSError, TypeError):
        pass

    # 2b) Resolved from pydme.__file__ (source / local -e install)
    try:
        import pydme
        pkg_dir = Path(pydme.__file__).resolve().parent / 'config'
        return (pkg_dir / 'blacklist.json').read_text(encoding='utf-8')
    except (ImportError, FileNotFoundError, OSError):
        pass

    return None


def is_risky(blacklist: Dict[str, list], topic: str, action_key: str) -> bool:
    """Whether ``action_key`` is on the blacklist for ``topic``."""
    return topic in blacklist and action_key in blacklist[topic]


def accept_risk_enabled(args: Any = None) -> bool:
    """Whether risk has been accepted: ``--accept-risk`` or ``DME_ACCEPT_RISK=true/1/yes``."""
    if args is not None and getattr(args, 'accept_risk', False):
        return True
    return os.environ.get('DME_ACCEPT_RISK', '').lower() in ('true', '1', 'yes')

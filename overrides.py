"""Persistent user overrides stored in overrides.json."""
import json
from pathlib import Path
from typing import Any

_FILE = Path(__file__).parent / "overrides.json"

_DEFAULTS: dict[str, Any] = {
    "manual_nav": None,
    "manual_prices": {},   # {TICKER: {price, prev_close, currency}}
}


def load() -> dict:
    if _FILE.exists():
        try:
            return {**_DEFAULTS, **json.loads(_FILE.read_text())}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(data: dict) -> None:
    _FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

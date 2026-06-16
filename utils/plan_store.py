"""
File-based storage for large session data (plans, weather, search).
Flask's cookie session has a ~4KB limit; 10 plans with full fields exceeds it.
This module stores data in data/_<key>.json on the server side.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def save(key: str, data: Any) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    (_DATA_DIR / f"_{key}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load(key: str, default: Any = None) -> Any:
    path = _DATA_DIR / f"_{key}.json"
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

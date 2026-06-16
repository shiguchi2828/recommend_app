"""
File-based TTL cache for external API results (Places API, Gemini API).
Keeps API costs near zero by reusing results within the TTL window.

Cache key format: {prefix}_{sha256[:20]}
Files stored at: data/cache/{key}.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
DEFAULT_TTL_HOURS: float = 24.0


def make_key(prefix: str, *parts: str) -> str:
    """Build a stable, filesystem-safe cache key from prefix + variable parts."""
    raw = "|".join(str(p).strip().lower() for p in parts)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def cache_get(key: str, ttl_hours: float = DEFAULT_TTL_HOURS) -> Any | None:
    """
    Return cached value if it exists and is within TTL.
    Returns None on miss, expiry, or read error.
    """
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        logger.info("[Cache] MISS  key=%s", key)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age_h = (time.time() - float(data["ts"])) / 3600
        if age_h > ttl_hours:
            logger.info("[Cache] EXPIRED key=%s (%.1fh old, limit=%.0fh)", key, age_h, ttl_hours)
            return None
        logger.info("[Cache] HIT   key=%s (%.1fh old)", key, age_h)
        return data["value"]
    except Exception as e:
        logger.warning("[Cache] read error key=%s: %s", key, e)
        return None


def cache_set(key: str, value: Any) -> None:
    """Persist value to cache file. Silently ignores write errors."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _CACHE_DIR / f"{key}.json"
        path.write_text(
            json.dumps({"ts": time.time(), "value": value}, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("[Cache] SET   key=%s", key)
    except Exception as e:
        logger.warning("[Cache] write error key=%s: %s", key, e)


def cache_delete(key: str) -> None:
    """Delete a single cache entry if it exists."""
    path = _CACHE_DIR / f"{key}.json"
    try:
        if path.exists():
            path.unlink()
            logger.info("[Cache] DEL   key=%s", key)
    except Exception as e:
        logger.warning("[Cache] delete error key=%s: %s", key, e)


def cache_stats() -> dict[str, int]:
    """Return count of valid vs expired cache entries (for diagnostics)."""
    if not _CACHE_DIR.exists():
        return {"valid": 0, "expired": 0, "error": 0}
    valid = expired = error = 0
    for f in _CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            age_h = (time.time() - float(data["ts"])) / 3600
            if age_h <= DEFAULT_TTL_HOURS:
                valid += 1
            else:
                expired += 1
        except Exception:
            error += 1
    return {"valid": valid, "expired": expired, "error": error}

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
FAVORITES_PATH = BASE_DIR / "data" / "favorites.json"


def load_favorites() -> list[dict[str, Any]]:
    if not FAVORITES_PATH.exists():
        return []

    try:
        with FAVORITES_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []
    return data


def save_favorites(favorites: list[dict[str, Any]]) -> None:
    FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FAVORITES_PATH.open("w", encoding="utf-8") as file:
        json.dump(favorites, file, ensure_ascii=False, indent=2)


def favorite_ids() -> set[str]:
    return {str(item.get("id")) for item in load_favorites() if item.get("id")}


def is_favorite(plan_id: str) -> bool:
    return plan_id in favorite_ids()


def add_favorite(plan: dict[str, Any]) -> dict[str, Any]:
    favorites = load_favorites()
    plan_id = str(plan.get("id", ""))
    if not plan_id:
        raise ValueError("plan id is required")

    existing = next((item for item in favorites if item.get("id") == plan_id), None)
    if existing:
        return existing

    favorite = dict(plan)
    favorite["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    favorites.insert(0, favorite)
    save_favorites(favorites)
    return favorite


def remove_favorite(plan_id: str) -> bool:
    favorites = load_favorites()
    filtered = [item for item in favorites if item.get("id") != plan_id]
    changed = len(filtered) != len(favorites)
    if changed:
        save_favorites(filtered)
    return changed

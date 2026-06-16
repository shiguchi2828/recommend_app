"""
ユーザー提案履歴の永続管理サービス。
data/user_history.json にセッション別で保存する。
同じスポットが連続提案されないよう段階ペナルティを提供する。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from threading import Lock

_log = logging.getLogger(__name__)

_HISTORY_FILE = Path(__file__).resolve().parent.parent / "data" / "user_history.json"
_lock = Lock()

# 段階ペナルティ値
_PENALTY_LATEST = -1000.0  # 直前の提案スポット（必ず排除）
_PENALTY_7DAYS  = -100.0   # 過去7日以内
_PENALTY_30DAYS = -30.0    # 過去30日以内


def _load() -> dict:
    try:
        if _HISTORY_FILE.exists():
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning("[History] 読み込み失敗: %s", e)
    return {}


def _save(data: dict) -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        _log.warning("[History] 保存失敗: %s", e)


def add_spots(session_id: str, spot_names: list[str]) -> None:
    """提案したスポット名をセッション履歴へ追加する。"""
    if not session_id or not spot_names:
        return
    with _lock:
        data    = _load()
        today   = date.today().isoformat()
        session = data.setdefault(session_id, [])
        existing_today = {e["name"] for e in session if e.get("date") == today}
        for name in spot_names:
            if name and name not in existing_today:
                session.append({"name": name, "date": today})
                existing_today.add(name)
        # セッションごと最大200件に切り詰め
        data[session_id] = session[-200:]
        # セッション数が50を超えたら最古のものを削除
        if len(data) > 50:
            def _oldest_date(entries: list) -> str:
                return min((e.get("date", "9999") for e in entries), default="9999")
            oldest = min(data, key=lambda k: _oldest_date(data[k]))
            data.pop(oldest, None)
        _save(data)
        _log.info("[History] %d件追加 session=%s", len(spot_names), session_id[:8])


def get_penalty_map(session_id: str) -> dict[str, float]:
    """
    {スポット名: ペナルティ値} を返す。
    ペナルティが大きいほど (負の値) スコアリングで不利になる。
    """
    if not session_id:
        return {}
    with _lock:
        data = _load()
    session = data.get(session_id, [])
    if not session:
        return {}

    today   = date.today()
    penalty: dict[str, float] = {}

    # 直前の提案スポット（最大ペナルティ）
    if session:
        latest = session[-1]["name"]
        penalty[latest] = _PENALTY_LATEST

    for entry in session:
        name = entry.get("name", "")
        if not name:
            continue
        try:
            d     = date.fromisoformat(entry["date"])
            delta = (today - d).days
        except Exception:
            continue

        if delta <= 7:
            p = _PENALTY_7DAYS
        elif delta <= 30:
            p = _PENALTY_30DAYS
        else:
            continue  # 30日超は対象外

        # より厳しいペナルティのみ残す
        if penalty.get(name, 0.0) > p:
            penalty[name] = p

    return penalty


def get_history_names(session_id: str, days: int = 30) -> frozenset[str]:
    """直近 days 日分の提案スポット名セットを返す（履歴除外用）。"""
    if not session_id:
        return frozenset()
    with _lock:
        data = _load()
    session = data.get(session_id, [])
    today   = date.today()
    names: set[str] = set()
    for entry in session:
        name = entry.get("name", "")
        if not name:
            continue
        try:
            d = date.fromisoformat(entry["date"])
            if (today - d).days <= days:
                names.add(name)
        except Exception:
            continue
    return frozenset(names)

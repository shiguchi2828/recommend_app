"""
Gemini を使ったSNS・話題性・イベント情報取得サービス。
1時間キャッシュ付き。Grounding (Google Search) を試み、失敗時は知識ベース生成にフォールバック。

架空スポット生成禁止 — 必ず実在施設名のみ使用 (Geminiへの指示で担保)。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from threading import Lock
from typing import Any

_log = logging.getLogger(__name__)

_TREND_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL   = 3600  # 1時間
_cache_lock  = Lock()

TREND_TYPES: dict[str, tuple[str, int]] = {
    "sns_popular":  ("📸 SNS人気",       85),
    "new_opening":  ("🆕 新規オープン",   90),
    "event":        ("🎪 開催中イベント", 95),
    "local_hidden": ("💎 穴場",           70),
    "trending":     ("🔥 急上昇",         80),
}


def get_trend_data(location: str) -> dict[str, dict]:
    """
    {スポット名: {trend_type, trend_score, label, reason}} を返す。
    キャッシュ有効期間内は再取得しない。
    """
    today_str = date.today().isoformat()
    cache_key = f"{location}|{today_str}"

    with _cache_lock:
        entry = _TREND_CACHE.get(cache_key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            _log.info("[Trend] キャッシュHIT: %s (%d件)", location, len(entry[1]))
            return entry[1]

    result = _fetch(location)

    with _cache_lock:
        _TREND_CACHE[cache_key] = (time.monotonic(), result)
        # メモリ上限 (20エントリ)
        if len(_TREND_CACHE) > 20:
            oldest = min(_TREND_CACHE, key=lambda k: _TREND_CACHE[k][0])
            _TREND_CACHE.pop(oldest, None)

    _log.info("[Trend] 取得完了: %s → %d件", location, len(result))
    return result


def _fetch(location: str) -> dict[str, dict]:
    """Gemini で話題スポットを検索する。失敗時は空 dict を返す。"""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return {}

    try:
        from google import genai
        from google.genai import types as ggt

        model  = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        client = genai.Client(api_key=key)

        today = date.today()
        prompt = _build_prompt(location, today.year, today.month)

        # Google Search Grounding を試みる
        resp = None
        try:
            grounding = ggt.Tool(google_search=ggt.GoogleSearch())
            config    = ggt.GenerateContentConfig(tools=[grounding])
            resp = client.models.generate_content(
                model=model, contents=prompt, config=config,
            )
            _log.info("[Trend] Grounding成功")
        except Exception as e:
            _log.info("[Trend] Grounding不可 (%s)、通常モードで再試行", type(e).__name__)

        if resp is None:
            resp = client.models.generate_content(model=model, contents=prompt)

        return _parse(resp.text if resp else "")

    except Exception as exc:
        _log.warning("[Trend] 取得失敗 (%s): %s", location, exc)
        return {}


def _build_prompt(location: str, year: int, month: int) -> str:
    return f"""あなたは{location}の地域情報に精通した専門家です。
{year}年{month}月現在、{location}で実際に話題になっているスポットを調べてください。

以下の5カテゴリで合計15〜20件、実在するスポット名を返してください:

1. SNS（Instagram/TikTok）で今月話題のスポット — 5件
2. 最近1年以内にオープンした新店舗・新施設 — 3〜5件
3. 今月または今週開催中のイベント・期間限定スポット — 3〜5件
4. 地元民に人気の穴場スポット（観光客に知られていない） — 3〜5件

【必須ルール】
- 必ず{location}内の実在する施設・場所のみ（架空スポット・テンプレート禁止）
- 「近くのカフェ」「〇〇付近の施設」などの一般名詞禁止
- Google Mapsで検索できる固有名詞のみ

JSONのみで返答（説明・前置き不要）:
{{"spots":[
  {{"name":"施設の正式名称","trend_type":"sns_popular|new_opening|event|local_hidden|trending","trend_score":0-100,"reason":"話題の理由を1文"}},
  ...
]}}"""


def _parse(text: str) -> dict[str, dict]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").lstrip("json").strip()

    s = cleaned.find("{")
    e = cleaned.rfind("}")
    if s == -1 or e == -1:
        return {}

    try:
        data   = json.loads(cleaned[s:e + 1])
        spots  = data.get("spots", [])
        result: dict[str, dict] = {}
        for item in spots:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or len(name) < 2:
                continue
            ttype = str(item.get("trend_type") or "trending")
            if ttype not in TREND_TYPES:
                ttype = "trending"
            try:
                score = max(0, min(100, int(item.get("trend_score") or 75)))
            except (TypeError, ValueError):
                score = 75
            label, _ = TREND_TYPES[ttype]
            result[name] = {
                "trend_type":  ttype,
                "trend_score": score,
                "label":       label,
                "reason":      str(item.get("reason") or ""),
            }
        return result
    except Exception as exc:
        _log.warning("[Trend] パース失敗: %s", exc)
        return {}


def apply_trend_to_spots(
    spots: list[dict[str, Any]],
    trend_data: dict[str, dict],
) -> list[dict[str, Any]]:
    """
    Places スポットリストへ trend_type / trend_score / trend_label を付与する。
    スポット名が trend_data に含まれる場合のみ付与 (なければ変更なし)。
    """
    if not trend_data:
        return spots
    result = []
    for spot in spots:
        name = spot.get("name", "")
        td   = trend_data.get(name) or trend_data.get(name.strip())
        if td:
            spot = dict(spot)
            spot["trend_type"]  = td["trend_type"]
            spot["trend_score"] = td["trend_score"]
            spot["trend_label"] = td["label"]
            spot["trend_reason"] = td["reason"]
            _log.debug("[Trend] %s → %s (%d)", name, td["label"], td["trend_score"])
        result.append(spot)
    return result

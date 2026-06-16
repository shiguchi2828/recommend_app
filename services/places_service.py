"""
Google Places API (Legacy) — fetch real candidate spots for a prefecture.

Changes vs previous version:
- Timeout: 10s → 3s
- Max queries: 7 → 3
- Sequential → parallel (ThreadPoolExecutor)
- REQUEST_DENIED: fail-fast + 1h cache, no 7x retry
- Timing log: [Timing] Places取得: X秒
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_BASE    = "https://maps.googleapis.com/maps/api/place"
_TIMEOUT = 3   # 10s → 3s

# ── 起動時テスト結果 ──────────────────────────────────────────────────────────
_PLACES_API_DISABLED      = False
_PLACES_API_DISABLE_REASON = ""


def check_places_api() -> tuple[bool, str]:
    """アプリ起動時に Places API の疎通確認を行う。
    REQUEST_DENIED ならモジュール全体を無効化し (ok=False, reason) を返す。
    ネットワーク例外は一時障害扱いで無効化しない。
    """
    global _PLACES_API_DISABLED, _PLACES_API_DISABLE_REASON

    api_key = (os.getenv("PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        _PLACES_API_DISABLED      = True
        _PLACES_API_DISABLE_REASON = "PLACES_API_KEY が未設定です"
        logger.warning("[Places] 起動テスト SKIP: %s", _PLACES_API_DISABLE_REASON)
        return False, _PLACES_API_DISABLE_REASON

    logger.info("[Places] 起動時接続テスト開始 (key=%s...)", api_key[:8])
    try:
        resp = requests.get(
            f"{_BASE}/textsearch/json",
            params={"query": "test", "language": "ja", "key": api_key},
            timeout=5,
        )
        data   = resp.json()
        status = data.get("status", "UNKNOWN")

        if status == "REQUEST_DENIED":
            msg       = data.get("error_message", "詳細不明")
            msg_lower = msg.lower()

            # キー制限の種類を特定してログに出力
            if "referer" in msg_lower or "http" in msg_lower:
                key_restriction = "Webサイト制限 (HTTP Referer)"
                fix_guide = (
                    "Google Cloud Console → 認証情報 → APIキーを選択 → "
                    "「アプリケーションの制限」を「なし」に変更してください"
                )
            elif "ip" in msg_lower:
                key_restriction = "IP制限 (IPアドレス制限)"
                fix_guide = (
                    "Google Cloud Console → 認証情報 → APIキーを選択 → "
                    "「IPアドレスの制限」にサーバーのIPアドレスを追加してください"
                )
            elif "api" in msg_lower and ("not" in msg_lower or "enable" in msg_lower):
                key_restriction = "API未有効"
                fix_guide = (
                    "Google Cloud Console → APIとサービス → ライブラリ → "
                    "「Places API」を検索して有効化してください"
                )
            else:
                key_restriction = f"拒否 (原因不明: {msg})"
                fix_guide = (
                    "Google Cloud Console → 認証情報 → APIキーの制限を確認し、"
                    "Places API を許可してください"
                )

            logger.error("[Places] 起動テスト FAILED")
            logger.error("[Places] キー種別: %s", key_restriction)
            logger.error("[Places] エラー詳細: %s", msg)
            logger.error("[Places] 修正方法: %s", fix_guide)

            ui_message = (
                f"Places API が使用できません ({key_restriction})。"
                f"Google Cloud ConsoleでAPIキー制限を解除してください。"
                f"手順: {fix_guide}"
            )
            _PLACES_API_DISABLED       = True
            _PLACES_API_DISABLE_REASON = ui_message
            from utils.api_cache import cache_set, make_key
            cache_set(make_key("places_deny", api_key[:10]), {"denied": True})
            return False, ui_message

        logger.info("[Places] 起動テスト OK (status=%s)", status)
        # 成功確定 → 無効フラグをリセットし、古い deny キャッシュを削除する
        _PLACES_API_DISABLED      = False
        _PLACES_API_DISABLE_REASON = ""
        from utils.api_cache import cache_delete, make_key
        cache_delete(make_key("places_deny", api_key[:10]))
        logger.info("[Places] 起動テスト成功 → denyキャッシュ削除")
        logger.info("[Places] Places API 利用可能")
        return True, ""

    except Exception as exc:
        logger.warning("[Places] 起動テスト 例外 (一時障害扱い、無効化しない): %s", exc)
        _PLACES_API_DISABLED      = False
        _PLACES_API_DISABLE_REASON = ""
        return True, ""

# ── Per-companion search terms ────────────────────────────────────────────────

_PEOPLE_QUERIES: dict[str, list[str]] = {
    "カップル": [
        "デートスポット",
        "夜景 スポット",
        "映えカフェ",
        "隠れ家 レストラン",
        "体験 工房 カップル",
    ],
    "家族": [
        "子供 遊び場 体験施設",
        "動物 ふれあい",
        "科学館 プラネタリウム",
        "アスレチック 公園",
    ],
    "友達": [
        "食べ歩き グルメ",
        "居酒屋 話題",
        "アクティビティ 体験",
        "サウナ スパ",
    ],
    "ひとり": [
        "こだわり カフェ",
        "温泉 日帰り",
        "美術館 ギャラリー",
        "自然 ハイキング 絶景",
    ],
}

# ── Per-purpose search terms ──────────────────────────────────────────────────

_PURPOSE_QUERIES: dict[str, list[str]] = {
    "デート":    ["デートスポット 夜景", "映えカフェ ロマンチック"],
    "SNS映え":   ["インスタ映え スポット", "フォトジェニック カフェ"],
    "食べ歩き":  ["食べ歩き グルメ", "居酒屋 人気", "市場 グルメ"],
    "自然":      ["自然 公園 絶景", "ハイキング 渓谷"],
    "ゆっくり":  ["温泉 日帰り", "カフェ のんびり"],
}


# ─────────────────────────────────────────────────────────────────────────────

def fetch_spots(
    place: str,
    people: str = "",
    purpose: str = "",
    weather_indoor: bool = False,
    max_per_query: int = 10,
) -> list[dict[str, Any]]:
    """
    Query Google Places API and return up to 60 real spot candidates.
    - 8 parallel queries
    - 3s per-request timeout
    - REQUEST_DENIED → fail-fast + 1-hour cache, skip remaining queries
    - Returns [] on any failure → caller falls back to Gemini/DB
    """
    # ── 0. 起動テストで無効化済みなら即リターン ───────────────────────────────
    if _PLACES_API_DISABLED:
        logger.warning("[Places] API無効 (%s) → スキップ", _PLACES_API_DISABLE_REASON)
        return []

    from utils.api_cache import cache_get, cache_set, make_key

    # ── 1. 結果キャッシュ確認（24時間） ────────────────────────────────────────
    cache_key = make_key("places", place, people, purpose, str(weather_indoor))
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info("[Places] キャッシュHIT → %d件 (Places API 未呼び出し)", len(cached))
        return cached
    logger.info("[Places] キャッシュMISS → 並列検索開始")

    # ── 2. APIキー確認 ─────────────────────────────────────────────────────────
    api_key = (os.getenv("PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        logger.warning("[Places] APIキー未設定 → スキップ")
        return []

    # ── 3. REQUEST_DENIED fail-fast キャッシュ確認（1時間） ────────────────────
    deny_key = make_key("places_deny", api_key[:10])
    if cache_get(deny_key, ttl_hours=1.0) is not None:
        logger.warning("[Places] 直近でREQUEST_DENIEDを記録 → 1時間スキップ")
        return []

    # ── 4. クエリ構築（最大8件） ───────────────────────────────────────────────
    queries  = _build_queries(place, people, purpose, weather_indoor)
    selected = queries[:8]
    logger.info("[Places] 並列検索: %d件 %s", len(selected), selected)

    # ── 5. 並列実行 ────────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    seen: set[str]             = set()
    spots: list[dict[str, Any]] = []
    request_denied             = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map: dict[concurrent.futures.Future, str] = {
            executor.submit(_text_search, q, api_key): q for q in selected
        }
        try:
            for future in concurrent.futures.as_completed(future_map, timeout=8.0):
                query = future_map[future]
                try:
                    status, results = future.result()
                except Exception as exc:
                    logger.warning("[Places] '%s' 例外: %s", query, exc)
                    continue

                if status == "REQUEST_DENIED":
                    request_denied = True
                    logger.warning(
                        "[Places] REQUEST_DENIED (referer/IP制限 or API未有効) → 即中断"
                    )
                    # 残りのfutureをキャンセル（応答を待たない）
                    for f in future_map:
                        f.cancel()
                    break

                added = 0
                for r in results[:max_per_query]:
                    pid = r.get("place_id", "")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    spot = _normalize(r, api_key)
                    if spot:
                        spots.append(spot)
                        added += 1
                logger.info("[Places] '%s' → %d件 (累計%d件)", query, added, len(spots))

        except concurrent.futures.TimeoutError:
            logger.warning("[Places] 並列取得 8s タイムアウト → 取得済みのみ使用")

    elapsed = time.perf_counter() - t0
    logger.info("[Timing] Places取得: %.1f秒 (%d件)", elapsed, len(spots))

    # ── 6. REQUEST_DENIED を1時間キャッシュ ────────────────────────────────────
    if request_denied:
        cache_set(deny_key, {"denied": True})
        logger.warning("[Places] REQUEST_DENIEDキャッシュ保存 → 1時間スキップ有効")
        return []

    # ── 7. ソート・上限・保存 ─────────────────────────────────────────────────
    spots.sort(key=lambda s: s.get("rating", 0), reverse=True)
    top = spots[:60]
    logger.info("[Places] 取得完了 %d件:", len(top))
    for s in top:
        logger.info("  ★%.1f  %s  (%s)", s.get("rating", 0), s["name"], s.get("address", "")[:30])

    if top:
        cache_set(cache_key, top)
        logger.info("[Places] キャッシュ保存完了")
    return top


def format_for_prompt(spots: list[dict[str, Any]]) -> str:
    """Format spots list into readable text for Gemini prompt."""
    if not spots:
        return "（Google Placesデータなし）"
    lines: list[str] = []
    for i, s in enumerate(spots[:25], 1):
        rating   = f"★{s['rating']:.1f}（{s['review_count']}件）" if s.get("rating") else "評価なし"
        price    = s.get("price_label", "")
        open_str = {"true": "営業中", "false": "閉店中"}.get(
            str(s.get("open_now")).lower(), "営業時間要確認"
        )
        line = (
            f"{i:2d}. {s['name']}\n"
            f"     評価: {rating}  {price}  {open_str}\n"
            f"     住所: {s['address']}"
        )
        lines.append(line)
    return "\n".join(lines)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_queries(
    place: str,
    people: str,
    purpose: str,
    indoor: bool,
) -> list[str]:
    base = [
        f"{place} 話題スポット SNS",
        f"{place} 人気 カフェ グルメ",
        f"{place} おすすめ 観光",
        f"{place} TikTok Instagram 映えスポット",
        f"{place} 2024 2025 新スポット 話題",
        f"{place} 穴場 地元 隠れ家 ローカル",
        f"{place} 季節 旬 限定 体験",
        f"{place} アクティブ 自然 公園 アウトドア",
    ]
    extra: list[str] = []
    for q in _PEOPLE_QUERIES.get(people, [])[:3]:
        extra.append(f"{place} {q}")
    for q in _PURPOSE_QUERIES.get(purpose, [])[:2]:
        extra.append(f"{place} {q}")
    if indoor:
        extra.append(f"{place} 屋内 施設 雨の日")
    return base + extra


def _text_search(query: str, api_key: str) -> tuple[str, list[dict]]:
    """
    Returns (status, results).
    status = "OK" | "ZERO_RESULTS" | "REQUEST_DENIED" | "EXCEPTION" | other
    All exceptions are caught and returned as ("EXCEPTION", []).
    """
    try:
        resp = requests.get(
            f"{_BASE}/textsearch/json",
            params={
                "query":    query,
                "language": "ja",
                "region":   "jp",
                "key":      api_key,
            },
            timeout=_TIMEOUT,
        )
        data   = resp.json()
        status = data.get("status", "UNKNOWN")
        if status == "REQUEST_DENIED":
            logger.warning(
                "[Places] REQUEST_DENIED: %s", data.get("error_message", "(no message)")
            )
            return status, []
        if status not in ("OK", "ZERO_RESULTS"):
            logger.warning("[Places] APIエラー status=%s", status)
            return status, []
        return status, data.get("results", [])
    except Exception as exc:
        logger.warning("[Places] リクエスト例外 query='%s': %s", query, exc)
        return "EXCEPTION", []


_PRICE_LABELS = {0: "無料", 1: "〜¥500", 2: "¥500〜¥1,500", 3: "¥1,500〜¥3,000", 4: "¥3,000〜"}


# ── SNS人気度推定スコア ────────────────────────────────────────────────────────
# Instagram: 視覚的魅力・映えタイプに高スコア
_INSTAGRAM_TYPES: dict[str, int] = {
    "cafe": 25, "bakery": 22,
    "tourist_attraction": 25, "viewpoint": 30,
    "beach": 28, "natural_feature": 22, "park": 15,
    "art_gallery": 22, "museum": 12,
    "restaurant": 15, "aquarium": 20,
    "amusement_park": 18, "spa": 12,
    "food": 12, "bar": 8,
}

# TikTok: 体験・グルメ・トレンド系に高スコア
_TIKTOK_TYPES: dict[str, int] = {
    "amusement_park": 30, "zoo": 25, "aquarium": 25,
    "food": 25, "restaurant": 22, "meal_delivery": 15,
    "tourist_attraction": 22, "natural_feature": 18,
    "beach": 20, "park": 15, "cafe": 18, "bakery": 15,
    "gym": 20, "stadium": 18,
}


def _calc_instagram_score(types: list[str], rating: float, review_count: int) -> int:
    """スポットタイプ・評価・レビュー数からInstagram人気度を推定（0–100）。"""
    type_bonus = max((_INSTAGRAM_TYPES.get(t, 0) for t in types), default=0)
    if review_count > 10000:
        review_bonus = 35
    elif review_count > 5000:
        review_bonus = 28
    elif review_count > 1000:
        review_bonus = 18
    elif review_count > 300:
        review_bonus = 10
    else:
        review_bonus = 3
    rating_bonus = min(30, int(rating * 6))
    return min(100, type_bonus + review_bonus + rating_bonus)


def _calc_tiktok_score(types: list[str], rating: float, review_count: int) -> int:
    """スポットタイプ・評価・レビュー数からTikTok人気度を推定（0–100）。"""
    type_bonus = max((_TIKTOK_TYPES.get(t, 0) for t in types), default=0)
    if review_count > 10000:
        review_bonus = 40
    elif review_count > 5000:
        review_bonus = 32
    elif review_count > 1000:
        review_bonus = 20
    elif review_count > 300:
        review_bonus = 12
    else:
        review_bonus = 5
    rating_bonus = min(20, int(rating * 4))
    return min(100, type_bonus + review_bonus + rating_bonus)


def _calc_popularity_score(ig: int, tt: int, rating: float, review_count: int) -> float:
    """Instagram・TikTok・Google評価を統合した総合人気スコア（0–100）。"""
    sns = (ig + tt) / 2
    if review_count > 10000:
        review_norm = 30
    elif review_count > 5000:
        review_norm = 24
    elif review_count > 1000:
        review_norm = 15
    elif review_count > 300:
        review_norm = 8
    else:
        review_norm = 2
    score = sns * 0.5 + review_norm + (rating / 5.0) * 20
    return round(min(100.0, score), 1)


# ── スポット属性タグ ──────────────────────────────────────────────────────────

_TAG_TYPE_MAP: dict[str, set[str]] = {
    "couple":   {"cafe", "restaurant", "spa", "beach", "aquarium",
                 "art_gallery", "tourist_attraction", "viewpoint"},
    "family":   {"park", "zoo", "aquarium", "amusement_park",
                 "museum", "shopping_mall", "natural_feature"},
    "solo":     {"cafe", "museum", "art_gallery", "spa", "park",
                 "natural_feature", "bakery"},
    "friends":  {"restaurant", "food", "bar", "amusement_park",
                 "tourist_attraction", "gym", "stadium"},
    "gourmet":  {"restaurant", "food", "cafe", "bakery", "bar", "meal_delivery"},
    "photo":    {"tourist_attraction", "art_gallery", "cafe", "park",
                 "beach", "viewpoint", "natural_feature"},
    "scenic":   {"park", "natural_feature", "beach", "viewpoint", "tourist_attraction"},
    "indoor":   {"museum", "art_gallery", "shopping_mall", "spa", "cafe",
                 "aquarium", "movie_theater"},
    "outdoor":  {"park", "natural_feature", "beach", "campground",
                 "zoo", "amusement_park"},
}


def _calc_tags(types: list[str], rating: float, review_count: int) -> list[str]:
    """スポットのタイプ・評価・レビュー数から属性タグを付与する。"""
    type_set = set(types)
    tags: list[str] = []
    for tag, tag_types in _TAG_TYPE_MAP.items():
        if type_set & tag_types:
            tags.append(tag)
    # 穴場: レビュー数が少なく評価が高いスポット
    if review_count < 300 and rating >= 3.8:
        tags.append("hidden_gem")
    return tags


def _normalize(r: dict, api_key: str) -> dict[str, Any] | None:
    name = (r.get("name") or "").strip()
    if not name:
        return None

    addr    = r.get("formatted_address") or r.get("vicinity") or ""
    rating  = float(r.get("rating") or 0)
    reviews = int(r.get("user_ratings_total") or 0)
    pl      = r.get("price_level")

    places_key = (os.getenv("PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    photos     = r.get("photos") or []
    photo_url  = ""
    if photos and places_key:
        ref = photos[0].get("photo_reference", "")
        if ref:
            photo_url = f"{_BASE}/photo?maxwidth=800&photoreference={ref}&key={places_key}"

    price = _PRICE_LABELS.get(pl, "不明") if pl is not None else "不明"

    geometry  = r.get("geometry") or {}
    loc       = geometry.get("location") or {}
    types_raw = r.get("types", [])

    ig   = _calc_instagram_score(types_raw, rating, reviews)
    tt   = _calc_tiktok_score(types_raw, rating, reviews)
    pop  = _calc_popularity_score(ig, tt, rating, reviews)
    tags = _calc_tags(types_raw, rating, reviews)

    return {
        "name":             name,
        "address":          addr,
        "rating":           rating,
        "review_count":     reviews,
        "place_id":         r.get("place_id", ""),
        "types":            types_raw,
        "photo_url":        photo_url,
        "open_now":         (r.get("opening_hours") or {}).get("open_now"),
        "price_label":      price,
        "price":            price,
        "latitude":         loc.get("lat"),
        "longitude":        loc.get("lng"),
        "instagram_score":  ig,
        "tiktok_score":     tt,
        "popularity_score": pop,
        "tags":             tags,
    }

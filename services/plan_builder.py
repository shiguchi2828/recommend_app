"""
Plan builder — Area-cluster based 1-day course generation.

Key design:
- Spots are clustered geographically into 2–3 areas (e.g. 別府/湯布院/大分市)
- Each area gets its own themed courses → 0% spot overlap between areas
- Within an area, round-robin theme assignment + strong dedup penalty keeps overlap <30%
- Travel time uses 30km/h (car/transit) with max 45-min hop filter
- Spot attribute tags (couple/family/solo/friends/gourmet/photo/scenic/indoor/outdoor/hidden_gem)
  feed directly into scoring
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from datetime import datetime
from hashlib import sha1
from typing import Any
from urllib.parse import quote, quote_plus

from services.scheduler import from_min, ideal_spot_count, schedule_spots, to_min

logger = logging.getLogger(__name__)


# ── Dynamic plan themes (condition-tagged pool) ───────────────────────────────
# Each theme declares which conditions make it applicable:
#   weather_ok : set of "sunny"/"cloudy"/"rainy"  — empty = any weather
#   people_ok  : set of companion types            — empty = any
#   budget_ok  : set of "low"/"mid"/"high"         — empty = any budget
# _select_themes_dynamic() filters this pool and scores/ranks by fit.

ALL_THEMES: list[dict] = [
    # ── 晴れ系 ──────────────────────────────────────────────────────────────
    {
        "key": "photo",
        "title": "写真映えコース",
        "description": "SNS映えスポットを巡るフォトジェニックな1日プラン",
        "prefer_types": {"tourist_attraction", "art_gallery", "park", "cafe",
                         "viewpoint", "beach"},
        "spot_tags":    {"photo", "couple", "scenic"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 8,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["映え", "SNS", "フォトジェニック"],
        "reason": "SNS映えスポットを中心にフォトジェニックなコースを提案しました。",
    },
    {
        "key": "scenic",
        "title": "絶景コース",
        "description": "晴れた空の下で絶景・自然スポットを巡るプラン",
        "prefer_types": {"natural_feature", "park", "beach", "viewpoint",
                         "tourist_attraction"},
        "spot_tags":    {"scenic", "outdoor", "photo"},
        "weather_ok":   {"sunny"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 9,
        "prefer_indoor": False,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["絶景", "自然", "晴れ"],
        "reason": "晴天のため絶景・自然スポットを中心にプランしました。",
    },
    {
        "key": "drive",
        "title": "ドライブ満喫コース",
        "description": "ドライブを楽しみながら絶景や名所を巡るプラン",
        "prefer_types": {"natural_feature", "park", "tourist_attraction",
                         "viewpoint", "beach"},
        "spot_tags":    {"scenic", "outdoor", "couple"},
        "weather_ok":   {"sunny"},
        "people_ok":    {"カップル", "友達", "ひとり"},
        "budget_ok":    set(),
        "base_priority": 7,
        "prefer_indoor": False,
        "prefer_budget_low": False,
        "tags": ["ドライブ", "絶景", "アウトドア"],
        "reason": "晴天のドライブ日和に合わせて絶景スポットを車で巡るコースを提案しました。",
    },
    {
        "key": "active",
        "title": "アクティブ探索コース",
        "description": "体を動かしながら充実した時間を過ごすアウトドアプラン",
        "prefer_types": {"park", "natural_feature", "amusement_park",
                         "gym", "stadium", "zoo", "campground"},
        "spot_tags":    {"outdoor", "friends", "family"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    {"友達", "家族", "ひとり"},
        "budget_ok":    set(),
        "base_priority": 7,
        "prefer_indoor": False,
        "prefer_budget_low": False,
        "tags": ["アクティブ", "アウトドア", "体験"],
        "reason": "屋外アクティビティを楽しめる天気のためアクティブコースを提案しました。",
    },
    # ── 雨系 ────────────────────────────────────────────────────────────────
    {
        "key": "rain",
        "title": "雨の日でも安心コース",
        "description": "天気を気にせず楽しめる屋内スポット中心の快適プラン",
        "prefer_types": {"museum", "art_gallery", "movie_theater", "cafe",
                         "shopping_mall", "spa", "aquarium"},
        "spot_tags":    {"indoor"},
        "weather_ok":   {"rainy"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 10,
        "prefer_indoor": True,
        "prefer_budget_low": False,
        "tags": ["雨OK", "屋内", "快適"],
        "reason": "雨予報のため屋内施設を中心としたコースを提案しました。",
    },
    {
        "key": "indoor",
        "title": "室内中心コース",
        "description": "快適な室内スポットでゆっくり過ごす文化的な1日プラン",
        "prefer_types": {"museum", "art_gallery", "shopping_mall",
                         "cafe", "spa", "movie_theater", "aquarium"},
        "spot_tags":    {"indoor", "solo", "family"},
        "weather_ok":   {"rainy", "cloudy"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 8,
        "prefer_indoor": True,
        "prefer_budget_low": False,
        "tags": ["室内", "快適", "文化"],
        "reason": "天候を考慮して快適に過ごせる室内施設中心のコースを提案しました。",
    },
    {
        "key": "shopping",
        "title": "ショッピングコース",
        "description": "雨の日も快適に楽しめるショッピング中心のプラン",
        "prefer_types": {"shopping_mall", "store", "cafe", "restaurant"},
        "spot_tags":    {"indoor"},
        "weather_ok":   {"rainy", "cloudy"},
        "people_ok":    {"カップル", "家族", "友達"},
        "budget_ok":    set(),
        "base_priority": 7,
        "prefer_indoor": True,
        "prefer_budget_low": False,
        "tags": ["ショッピング", "雨OK", "屋内"],
        "reason": "雨天のため屋内で楽しめるショッピングコースを提案しました。",
    },
    {
        "key": "cafe_hopping",
        "title": "カフェ巡りコース",
        "description": "個性的なカフェを巡るおしゃれな1日プラン",
        "prefer_types": {"cafe", "bakery", "restaurant"},
        "spot_tags":    {"gourmet", "couple", "solo"},
        "weather_ok":   {"rainy", "cloudy", "sunny"},
        "people_ok":    {"カップル", "ひとり", "友達"},
        "budget_ok":    set(),
        "base_priority": 7,
        "prefer_indoor": True,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["カフェ", "おしゃれ", "のんびり"],
        "reason": "雰囲気の良いカフェをめぐるカフェ巡りコースを提案しました。",
    },
    # ── カップル系 ───────────────────────────────────────────────────────────
    {
        "key": "date",
        "title": "デートコース",
        "description": "二人でゆったりと過ごせるロマンチックなデートプラン",
        "prefer_types": {"cafe", "restaurant", "tourist_attraction",
                         "art_gallery", "spa", "aquarium", "viewpoint"},
        "spot_tags":    {"couple", "photo", "scenic"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    {"カップル"},
        "budget_ok":    set(),
        "base_priority": 10,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["デート", "カップル", "ロマンチック"],
        "reason": "カップル利用のため雰囲気の良いデートスポットを中心にコースを提案しました。",
    },
    {
        "key": "night_view",
        "title": "夜景・夕暮れコース",
        "description": "夕日や夜景スポットを巡るロマンチックなプラン",
        "prefer_types": {"tourist_attraction", "viewpoint", "restaurant", "cafe"},
        "spot_tags":    {"couple", "scenic", "photo"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    {"カップル"},
        "budget_ok":    set(),
        "base_priority": 9,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["夜景", "夕日", "ロマンチック"],
        "reason": "カップル利用のため夕日・夜景スポットを含むロマンチックなコースを提案しました。",
    },
    # ── 家族系 ───────────────────────────────────────────────────────────────
    {
        "key": "family",
        "title": "ファミリーコース",
        "description": "家族全員で楽しめる充実の1日プラン",
        "prefer_types": {"park", "zoo", "aquarium", "amusement_park",
                         "museum", "shopping_mall"},
        "spot_tags":    {"family", "outdoor"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    {"家族"},
        "budget_ok":    set(),
        "base_priority": 10,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "tags": ["ファミリー", "子供", "体験"],
        "reason": "ご家族でのご利用のため子供も大人も楽しめるファミリーコースを提案しました。",
    },
    {
        "key": "experience",
        "title": "体験施設コース",
        "description": "体験・アクティビティ中心の楽しいプラン",
        "prefer_types": {"amusement_park", "zoo", "aquarium", "museum", "gym"},
        "spot_tags":    {"family", "friends"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    {"家族", "友達"},
        "budget_ok":    set(),
        "base_priority": 8,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "tags": ["体験", "アクティビティ", "楽しい"],
        "reason": "体験・アクティビティ施設を中心にコースを提案しました。",
    },
    {
        "key": "aquarium",
        "title": "水族館・動物園コース",
        "description": "水族館・動物園など生き物と触れ合える1日プラン",
        "prefer_types": {"aquarium", "zoo", "natural_feature", "park"},
        "spot_tags":    {"family", "couple"},
        "weather_ok":   {"rainy", "cloudy", "sunny"},
        "people_ok":    {"家族", "カップル"},
        "budget_ok":    set(),
        "base_priority": 8,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "tags": ["水族館", "動物園", "体験"],
        "reason": "生き物と触れ合える水族館・動物園を中心にコースを提案しました。",
    },
    # ── 一人系 ───────────────────────────────────────────────────────────────
    {
        "key": "onsen",
        "title": "温泉巡りコース",
        "description": "地域の温泉・スパをゆったり楽しむ癒しプラン",
        "prefer_types": {"spa", "natural_feature", "restaurant", "cafe"},
        "spot_tags":    {"solo", "couple"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    {"ひとり", "カップル"},
        "budget_ok":    set(),
        "base_priority": 9,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "tags": ["温泉", "癒し", "のんびり"],
        "reason": "温泉・スパを中心にゆったり過ごせるコースを提案しました。",
    },
    {
        "key": "walking",
        "title": "散策コース",
        "description": "気ままに歩いて発見する地域の魅力プラン",
        "prefer_types": {"park", "natural_feature", "tourist_attraction",
                         "cafe", "shrine", "temple"},
        "spot_tags":    {"solo", "outdoor", "hidden_gem"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    {"ひとり", "カップル", "友達"},
        "budget_ok":    set(),
        "base_priority": 9,
        "prefer_indoor": None,
        "prefer_budget_low": True,
        "tags": ["散策", "ひとり旅", "のんびり"],
        "reason": "おひとりさまのご利用のため自分のペースで楽しめる散策コースを提案しました。",
    },
    {
        "key": "relax",
        "title": "ゆったりのんびりコース",
        "description": "時間を気にせずゆっくり過ごす癒しとリラックスのプラン",
        "prefer_types": {"spa", "cafe", "park", "museum", "bakery"},
        "spot_tags":    {"solo", "couple"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    {"ひとり", "カップル", "友達"},
        "budget_ok":    set(),
        "base_priority": 6,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "slot_bonus": 15,
        "tags": ["ゆったり", "癒し", "のんびり"],
        "reason": "のんびりとした時間を楽しめるリラックスコースを提案しました。",
    },
    # ── 予算系 ───────────────────────────────────────────────────────────────
    {
        "key": "budget",
        "title": "コスパ重視コース",
        "description": "リーズナブルに楽しめるお得なスポットを厳選したプラン",
        "prefer_types": set(),
        "spot_tags":    {"hidden_gem"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    set(),
        "budget_ok":    {"low"},
        "base_priority": 9,
        "prefer_indoor": None,
        "prefer_budget_low": True,
        "tags": ["コスパ", "お得", "節約"],
        "reason": "予算を抑えてリーズナブルに楽しめるコスパ重視コースを提案しました。",
    },
    {
        "key": "free_spots",
        "title": "無料スポット巡り",
        "description": "入場無料・低コストで楽しめるスポットを厳選したプラン",
        "prefer_types": {"park", "natural_feature", "tourist_attraction", "beach"},
        "spot_tags":    {"outdoor", "scenic", "hidden_gem"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    set(),
        "budget_ok":    {"low"},
        "base_priority": 8,
        "prefer_indoor": False,
        "prefer_budget_low": True,
        "tags": ["無料", "コスパ", "アウトドア"],
        "reason": "予算を抑えるため無料・低コストスポットを中心に構成しました。",
    },
    # ── 汎用（予算mid/high優先） ──────────────────────────────────────────────
    {
        "key": "classic",
        "title": "人気観光コース",
        "description": "地域の名所・名物を余すことなく楽しむ王道観光プラン",
        "prefer_types": {"tourist_attraction", "museum", "park",
                         "natural_feature", "zoo", "aquarium"},
        "spot_tags":    {"scenic", "photo", "family"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    set(),
        "budget_ok":    {"mid", "high"},
        "base_priority": 8,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["観光", "定番", "名所"],
        "reason": "地域の人気スポットを巡る王道観光コースを提案しました。",
    },
    {
        "key": "gourmet",
        "title": "グルメ特化コース",
        "description": "地元の名物グルメとカフェを中心に楽しむ美食プラン",
        "prefer_types": {"restaurant", "food", "cafe", "bakery", "bar", "meal_delivery"},
        "spot_tags":    {"gourmet", "friends"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    set(),
        "budget_ok":    {"mid", "high"},
        "base_priority": 8,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["グルメ", "カフェ", "食事"],
        "reason": "地元の名物グルメを中心にめぐるグルメ特化コースを提案しました。",
    },
    {
        "key": "local",
        "title": "ローカル穴場コース",
        "description": "地元民しか知らない隠れた名店・穴場スポットを発掘するプラン",
        "prefer_types": {"cafe", "restaurant", "park", "store", "bakery"},
        "spot_tags":    {"hidden_gem", "solo", "gourmet"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 6,
        "prefer_indoor": None,
        "prefer_budget_low": True,
        "prefer_low_review_count": True,
        "tags": ["穴場", "ローカル", "隠れ家"],
        "reason": "地元民しか知らない穴場スポットを発掘するローカルコースを提案しました。",
    },
    # ── 季節限定 ──────────────────────────────────────────────────────────────
    {
        "key": "season",
        "title": "季節限定コース",
        "description": "今の季節ならではのスポット・体験を楽しむ旬なプラン",
        "prefer_types": {"park", "natural_feature", "tourist_attraction",
                         "cafe", "beach", "zoo", "aquarium"},
        "spot_tags":    {"scenic", "outdoor", "photo"},
        "weather_ok":   {"sunny", "cloudy"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 8,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["季節限定", "旬", "いまだけ"],
        "reason": "今の季節ならではのスポットを巡る季節限定コースを提案しました。",
    },
    # ── 新規オープン ─────────────────────────────────────────────────────────
    {
        "key": "new_open",
        "title": "新規オープン巡り",
        "description": "最近オープンしたばかりの新スポットをいち早く体験するプラン",
        "prefer_types": {"restaurant", "cafe", "store", "tourist_attraction",
                         "shopping_mall", "bakery", "bar"},
        "spot_tags":    {"photo", "gourmet", "hidden_gem"},
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 8,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_new_opening": True,
        "tags": ["新規", "オープン", "最新"],
        "reason": "最近オープンしたばかりの新スポットを中心にプランを提案しました。",
    },
    # ── AIおまかせ ───────────────────────────────────────────────────────────
    {
        "key": "ai_recommended",
        "title": "AIおまかせコース",
        "description": "AIが今日のコンディションで最適な組み合わせを厳選した特別プラン",
        "prefer_types": set(),
        "spot_tags":    set(),
        "weather_ok":   {"sunny", "cloudy", "rainy"},
        "people_ok":    set(),
        "budget_ok":    set(),
        "base_priority": 7,
        "prefer_indoor": None,
        "prefer_budget_low": False,
        "prefer_high_rating": True,
        "tags": ["AI厳選", "おまかせ", "バランス"],
        "reason": "AIが今日の条件に最適なスポットの組み合わせを厳選しました。",
    },
]

# Backward-compat alias used by older callers (e.g. tests)
THEMES = ALL_THEMES


# ── Type / tag classifications ────────────────────────────────────────────────

_OUTDOOR_TYPES = {
    "park", "natural_feature", "beach", "campground", "stadium",
    "amusement_park", "zoo", "tourist_attraction", "point_of_interest",
}
_INDOOR_TYPES = {
    "museum", "art_gallery", "shopping_mall", "spa", "cafe", "bakery",
    "restaurant", "food", "meal_delivery", "meal_takeaway", "movie_theater",
    "aquarium", "bar", "night_club", "lodging",
}

# Weather-optimization constants
_WIND_SENSITIVE_TYPES = {
    "viewpoint", "natural_feature", "beach", "campground", "stadium",
}
_NIGHTVIEW_TYPES = {
    "viewpoint", "tourist_attraction", "amusement_park",
}
_NIGHTVIEW_KEYWORDS = ("夜景", "展望", "タワー", "スカイ", "観覧車", "イルミ")


def _is_nightview(spot: dict) -> bool:
    name  = spot.get("name", "")
    types = set(spot.get("types") or [])
    return (
        any(kw in name for kw in _NIGHTVIEW_KEYWORDS)
        or bool(types & _NIGHTVIEW_TYPES)
    )


def _weather_issue(spot: dict, slot: dict | None) -> str | None:
    """Return the primary weather problem for a spot at this time slot, or None."""
    if slot is None:
        return None
    types       = set(spot.get("types") or [])
    rain        = slot.get("rain_chance", 0)
    temp        = slot.get("temperature", 20.0)
    wind        = slot.get("wind_speed", 0.0)

    # 35°C heat during 13-16h window for outdoor spots
    try:
        h = int(slot.get("time", "12:00").split(":")[0])
    except Exception:
        h = 12
    if temp >= 35 and 13 <= h <= 16 and types & _OUTDOOR_TYPES:
        return "heat"
    if rain >= 50 and types & _OUTDOOR_TYPES and not (types & _INDOOR_TYPES):
        return "rain"
    if wind >= 8.0 and types & _WIND_SENSITIVE_TYPES:
        return "wind"
    return None


def _find_indoor_replacement(
    spot: dict,
    cluster_spots: list[dict],
    used_global: set[str],
) -> dict | None:
    """Find the best unused indoor spot from the cluster to replace an outdoor spot."""
    candidates = [
        s for s in cluster_spots
        if s["name"] not in used_global
        and s["name"] != spot["name"]
        and bool(set(s.get("types") or []) & _INDOOR_TYPES)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: float(s.get("rating") or 0))


def _optimize_for_weather(
    built: list[dict],
    cluster_spots: list[dict],
    hourly: list[dict],
    used_global: set[str],
) -> tuple[list[dict], bool]:
    """
    Swap weather-problematic spots with indoor alternatives where possible.
    Returns (modified_built, was_optimized).
    """
    optimized = False
    result    = list(built)

    for i, spot in enumerate(result):
        slot  = _match_spot_weather(spot["start_time"], hourly)
        issue = _weather_issue(spot, slot)
        if issue is None:
            continue

        replacement = _find_indoor_replacement(spot, cluster_spots, used_global)
        if replacement is None:
            continue

        # Night-view boost: 18h+ clear sky → prefer nightview spots instead of swapping out
        if slot and slot.get("rain_chance", 0) < 30:
            try:
                h = int(spot["start_time"].split(":")[0])
            except Exception:
                h = 12
            if h >= 18 and _is_nightview(spot):
                continue  # keep night-view spot when clear at night

        name = replacement["name"]
        addr = replacement.get("address", "")
        pid  = replacement.get("place_id", "")
        new_spot = {
            **spot,
            "name":           name,
            "price":          replacement.get("price") or replacement.get("price_label") or "不明",
            "rating":         float(replacement.get("rating") or 4.0),
            "photo_url":      replacement.get("photo_url", ""),
            "address":        addr,
            "links":          _links(name, addr, pid),
            "types":          replacement.get("types", []),
            "source":         replacement.get("source", "places"),
            "category_label": _CATEGORY_LABELS_JA.get(_category_of(replacement), "スポット"),
            "spot_tags":      list(_get_spot_tags(replacement)),
            "latitude":       replacement.get("latitude"),
            "longitude":      replacement.get("longitude"),
            "popularity_score": replacement.get("popularity_score", 0),
            "instagram_score":  replacement.get("instagram_score", 0),
            "tiktok_score":     replacement.get("tiktok_score", 0),
            "estimated_cost":   _estimate_spot_cost(replacement),
            "is_indoor":        True,
        }
        result[i] = new_spot
        used_global.add(name)
        optimized = True
        logger.info(
            "[WeatherOpt] '%s' → '%s' (issue=%s, rain=%d%%, wind=%.1fm/s, temp=%.1f℃)",
            spot["name"], name, issue,
            slot.get("rain_chance", 0), slot.get("wind_speed", 0), slot.get("temperature", 0),
        )

    # Night-view promotion: 18h+ clear → move nightview spots to evening if available
    if hourly:
        for i, spot in enumerate(result):
            sw = _match_spot_weather(spot["start_time"], hourly)
            if sw and sw.get("rain_chance", 100) < 30:
                try:
                    h = int(spot["start_time"].split(":")[0])
                except Exception:
                    h = 0
                if h >= 18 and not _is_nightview(spot):
                    # find nightview candidate not already in plan
                    plan_names = {s["name"] for s in result}
                    nv_cands = [
                        s for s in cluster_spots
                        if _is_nightview(s)
                        and s["name"] not in used_global
                        and s["name"] not in plan_names
                    ]
                    if nv_cands:
                        nv = max(nv_cands, key=lambda s: float(s.get("rating") or 0))
                        name = nv["name"]
                        addr = nv.get("address", "")
                        pid  = nv.get("place_id", "")
                        result[i] = {
                            **spot,
                            "name":           name,
                            "price":          nv.get("price") or nv.get("price_label") or "不明",
                            "rating":         float(nv.get("rating") or 4.0),
                            "photo_url":      nv.get("photo_url", ""),
                            "address":        addr,
                            "links":          _links(name, addr, pid),
                            "types":          nv.get("types", []),
                            "source":         nv.get("source", "places"),
                            "category_label": _CATEGORY_LABELS_JA.get(_category_of(nv), "スポット"),
                            "spot_tags":      list(_get_spot_tags(nv)),
                            "latitude":       nv.get("latitude"),
                            "longitude":      nv.get("longitude"),
                            "popularity_score": nv.get("popularity_score", 0),
                            "instagram_score":  nv.get("instagram_score", 0),
                            "tiktok_score":     nv.get("tiktok_score", 0),
                            "estimated_cost":   _estimate_spot_cost(nv),
                            "is_indoor":        False,
                        }
                        used_global.add(name)
                        optimized = True
                        logger.info("[WeatherOpt] 夜景促進: '%s' → '%s'", spot["name"], name)
                        break  # only promote one nightview spot per plan

    return result, optimized


# Spot attribute tag → matching Google Places types
_SPOT_TAG_TYPES: dict[str, set[str]] = {
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
    "nature":   {"park", "natural_feature", "beach", "campground"},
    "history":  {"museum", "art_gallery", "tourist_attraction", "shrine",
                 "temple", "church"},
    # hidden_gem: determined by review_count, not types
}


def _get_spot_tags(spot: dict) -> set[str]:
    """Return the set of attribute tags for a spot."""
    tags: set[str] = set(spot.get("tags") or [])  # pre-computed by places_service
    if not tags:
        # Fallback: compute from types
        types = set(spot.get("types") or [])
        for tag, tag_types in _SPOT_TAG_TYPES.items():
            if types & tag_types:
                tags.add(tag)
        rc = int(spot.get("review_count") or 0)
        if rc < 300 and float(spot.get("rating") or 0) >= 3.8:
            tags.add("hidden_gem")
    return tags


# ── Companion / purpose preferences ──────────────────────────────────────────

_PEOPLE_PREFER: dict[str, set[str]] = {
    "カップル": {"cafe", "restaurant", "spa", "beach", "aquarium",
                "art_gallery", "tourist_attraction", "viewpoint"},
    "家族":     {"park", "zoo", "aquarium", "amusement_park",
                "museum", "shopping_mall", "natural_feature"},
    "友達":     {"restaurant", "food", "bar", "amusement_park",
                "tourist_attraction", "gym", "stadium", "natural_feature"},
    "ひとり":   {"cafe", "museum", "art_gallery", "spa", "park",
                "natural_feature", "bakery"},
}

_PEOPLE_AVOID: dict[str, set[str]] = {
    "カップル": {"zoo", "amusement_park"},
    "家族":     {"bar", "night_club", "spa"},
    "友達":     set(),
    "ひとり":   {"bar", "night_club", "amusement_park", "stadium"},
}

_PURPOSE_PREFER: dict[str, set[str]] = {
    # 現在のフォーム選択肢
    "自然":     {"park", "natural_feature", "beach", "campground"},
    "グルメ":   {"restaurant", "food", "cafe", "bakery", "bar", "meal_delivery"},
    "写真映え": {"tourist_attraction", "art_gallery", "cafe", "park",
                "beach", "natural_feature", "viewpoint"},
    "温泉":     {"spa", "natural_feature"},
    "歴史":     {"museum", "art_gallery", "tourist_attraction",
                "temple", "shrine", "church"},
    "アクティブ": {"amusement_park", "gym", "park", "natural_feature",
                 "stadium", "campground", "zoo"},
    "ゆっくり": {"cafe", "park", "spa", "museum", "natural_feature", "bakery"},
    "デート":   {"cafe", "tourist_attraction", "spa", "beach",
                "art_gallery", "viewpoint", "aquarium"},
    # レガシー互換
    "SNS映え":  {"tourist_attraction", "art_gallery", "cafe", "park",
                "beach", "natural_feature", "viewpoint"},
    "食べ歩き": {"restaurant", "food", "cafe", "bakery", "bar", "meal_delivery"},
}

_PURPOSE_AVOID: dict[str, set[str]] = {
    "自然":     {"shopping_mall", "store", "movie_theater"},
    "グルメ":   {"gym", "stadium", "campground", "natural_feature"},
    "歴史":     {"amusement_park", "gym", "stadium"},
    "アクティブ": {"museum", "art_gallery", "spa", "shopping_mall"},
    "温泉":     {"amusement_park", "gym", "stadium"},
    "ゆっくり": {"amusement_park", "gym", "stadium"},
    "デート":   {"zoo", "amusement_park"},
    "写真映え": set(),
    # レガシー互換
    "SNS映え":  set(),
    "食べ歩き": {"gym", "stadium", "campground"},
}

# ── Comments ──────────────────────────────────────────────────────────────────

_PEOPLE_COMMENTS: dict[str, str] = {
    "カップル": "カップルのご利用のため、雰囲気の良いカフェや絶景スポットを中心にデート向けコースを作成しました。",
    "家族":     "ご家族でのご利用のため、子供も楽しめる体験施設と公園を中心に選定しました。",
    "友達":     "友達グループのご利用のため、みんなで盛り上がれるアクティビティとグルメを中心に選びました。",
    "ひとり":   "おひとりさまのご利用のため、のんびり過ごせるカフェや散策・文化系スポットを選びました。",
}

_PURPOSE_COMMENTS: dict[str, str] = {
    "デート":     "景色と雰囲気を重視したデート向けプランです。",
    "のんびり":   "移動を少なめに、各スポットでゆっくり過ごせるプランです。",
    "アクティブ": "体験施設とアウトドアを組み合わせた充実のプランです。",
    "グルメ":     "飲食スポットを充実させたグルメ特化プランです。",
    "写真映え":   "SNS映えスポットを優先的に選んだフォトジェニックプランです。",
}

# ── Purpose → theme whitelist (hard filter) ─────────────────────────────────
# When purpose is set, ONLY themes in the whitelist are generated.
# This prevents 自然 users seeing ショッピングコース, グルメ users seeing 展望台コース, etc.
_PURPOSE_THEME_WHITELIST: dict[str, set[str]] = {
    "自然":     {"scenic", "drive", "active", "walking", "photo", "free_spots",
                "aquarium", "experience"},        # aquarium/experience = rainy fallback
    "グルメ":   {"gourmet", "cafe_hopping", "local", "budget", "relax"},
    "写真映え": {"photo", "scenic", "date", "night_view", "drive", "walking"},
    "温泉":     {"onsen", "relax", "local", "walking", "cafe_hopping"},
    "歴史":     {"classic", "local", "walking", "photo"},
    "アクティブ": {"active", "drive", "scenic", "family", "experience", "walking"},
    "ゆっくり": {"relax", "onsen", "cafe_hopping", "walking", "indoor", "local"},
    "デート":   {"date", "night_view", "cafe_hopping", "onsen", "aquarium", "photo"},
    # Legacy backward-compat
    "SNS映え":  {"photo", "scenic", "date", "night_view", "drive"},
    "食べ歩き": {"gourmet", "cafe_hopping", "local", "budget", "relax"},
}

# Indoor/rainy-safe themes added to any whitelist when weather is rainy
_RAINY_FALLBACK_THEMES: set[str] = {
    "rain", "indoor", "shopping", "cafe_hopping", "experience", "aquarium",
}

# ── Condition-aware reason lookup ────────────────────────────────────────────
# (weather_cat, theme_key) → reason text shown on each course card
_REASON_BY_WEATHER: dict[tuple[str, str], str] = {
    ("rainy",  "rain"):        "雨予報のため、雨でも安心して楽しめる屋内スポット中心のコースを提案しました。",
    ("rainy",  "indoor"):      "雨天のため、快適な室内施設でゆっくり過ごせるコースを提案しました。",
    ("rainy",  "shopping"):    "雨天のため、屋根のある施設でショッピングを楽しむコースを提案しました。",
    ("rainy",  "cafe_hopping"):"雨の日もゆっくりカフェでくつろぎながら地域の味を楽しむコースを提案しました。",
    ("rainy",  "aquarium"):    "雨天でも快適に楽しめる水族館・動物園中心のコースを提案しました。",
    ("rainy",  "experience"):  "雨天でも体験・アクティビティが充実した施設を中心にコースを提案しました。",
    ("sunny",  "scenic"):      "晴天のため、絶景・自然スポットを存分に楽しめるコースを提案しました。",
    ("sunny",  "drive"):       "晴天のドライブ日和に合わせて、車で絶景スポットを巡るコースを提案しました。",
    ("sunny",  "active"):      "晴天のため、屋外アクティビティを思い切り楽しめるコースを提案しました。",
    ("sunny",  "photo"):       "晴天の青空をバックに、写真映えスポットを巡るコースを提案しました。",
    ("sunny",  "family"):      "晴天のため、お子様が思い切り遊べるファミリーコースを提案しました。",
    ("sunny",  "free_spots"):  "晴天のため、無料・低コストで楽しめる屋外スポットを中心に構成しました。",
    ("cloudy", "photo"):       "曇り空でも映えるスポットを中心に、フォトジェニックコースを提案しました。",
}

# (people, theme_key) → reason text
_REASON_BY_PEOPLE: dict[tuple[str, str], str] = {
    ("カップル", "date"):        "カップル利用のため、雰囲気の良いスポットを組み込んだデートコースを提案しました。",
    ("カップル", "night_view"):  "カップル利用のため、夕日・夜景スポットを含むロマンチックなコースを提案しました。",
    ("カップル", "aquarium"):    "カップル利用のため、デートスポットとして人気の水族館コースを提案しました。",
    ("カップル", "onsen"):       "カップル利用のため、二人でのんびり過ごせる温泉巡りコースを提案しました。",
    ("カップル", "photo"):       "カップル利用のため、二人の思い出になる写真映えスポットを集めたコースを提案しました。",
    ("家族",    "family"):       "ご家族でのご利用のため、子供も大人も楽しめるファミリーコースを提案しました。",
    ("家族",    "aquarium"):     "ご家族でのご利用のため、子供が大喜びの水族館・動物園コースを提案しました。",
    ("家族",    "experience"):   "ご家族でのご利用のため、体験・アクティビティ施設を中心にコースを提案しました。",
    ("家族",    "shopping"):     "ご家族でのご利用のため、子供も楽しめるショッピングモール中心のコースを提案しました。",
    ("ひとり",  "onsen"):        "おひとりさまのご利用のため、のんびり温泉巡りを楽しむコースを提案しました。",
    ("ひとり",  "walking"):      "おひとりさまのご利用のため、自分のペースで街歩きを楽しむ散策コースを提案しました。",
    ("ひとり",  "cafe_hopping"): "おひとりさまのご利用のため、個性豊かなカフェをめぐるコースを提案しました。",
    ("ひとり",  "relax"):        "おひとりさまのご利用のため、ゆったりとした時間を過ごせるリラックスコースを提案しました。",
    ("友達",    "active"):       "友達グループのご利用のため、みんなで盛り上がれるアクティブコースを提案しました。",
    ("友達",    "experience"):   "友達グループのご利用のため、一緒に体験できるアクティビティ中心のコースを提案しました。",
    ("友達",    "gourmet"):      "友達グループのご利用のため、みんなで楽しめるグルメスポット中心のコースを提案しました。",
}


# ── Scoring ───────────────────────────────────────────────────────────────────

def _proximity_score(spot: dict, user_lat: float, user_lon: float) -> float:
    """
    ユーザーの現在地からスポットまでの距離に基づくスコアボーナス。
    近いほど高スコア → 現在地周辺スポットが優先される。
    """
    s_lat = spot.get("latitude")
    s_lon = spot.get("longitude")
    if not (s_lat and s_lon and user_lat and user_lon):
        return 0.0
    dist_km = _haversine(user_lat, user_lon, float(s_lat), float(s_lon))
    if dist_km <= 5:
        return 4.0    # 5km以内: 超優先
    if dist_km <= 15:
        return 3.0    # 15km以内: 近傍エリア
    if dist_km <= 30:
        return 2.0    # 30km以内: 同日圏
    if dist_km <= 50:
        return 1.0    # 50km以内: やや遠い
    return -2.0       # 50km超: 大幅減点（耶馬溪→別府などを防ぐ）


def _score(
    spot: dict,
    theme: dict,
    people: str,
    weather_rec: str = "",
    purpose: str = "",
    budget: str = "",
    used_names: set[str] | None = None,
    user_lat: float = 0.0,
    user_lon: float = 0.0,
    season: str = "",
    temperature: float = 20.0,
    history_names: frozenset[str] = frozenset(),
    penalty_map: dict | None = None,
    trend_data: dict | None = None,
) -> float:
    base   = float(spot.get("rating") or 3.5)
    types  = set(spot.get("types") or [])
    prefer: set = theme.get("prefer_types") or set()
    score  = base

    # ─── 履歴ペナルティ（最優先: -1000/-100/-30）──────────────────────────────
    if penalty_map:
        name_pen = penalty_map.get(spot.get("name", ""), 0.0)
        if name_pen != 0.0:
            score += name_pen  # 大きな負値で事実上除外

    # ─── SNS・トレンドスコア（SNS話題性 × 0.30 に相当）─────────────────────
    ts = int(spot.get("trend_score") or 0)
    if ts == 0 and trend_data:
        td = trend_data.get(spot.get("name", ""))
        if td:
            ts = int(td.get("trend_score") or 0)
    ttype = spot.get("trend_type") or (trend_data.get(spot.get("name", ""), {}).get("trend_type") if trend_data else None) or ""

    if ts >= 90:
        score += 8.0
    elif ts >= 80:
        score += 5.5
    elif ts >= 70:
        score += 3.5
    elif ts >= 60:
        score += 1.5

    # イベント性ボーナス（イベント性 × 0.20 に相当）
    if ttype == "event":
        score += 5.0
    elif ttype == "new_opening":
        score += 3.0
    elif ttype == "local_hidden":
        score += 2.0
    elif ttype == "trending":
        score += 1.5

    # 新規オープン巡りテーマ専用ボーナス
    if theme.get("prefer_new_opening") and ttype == "new_opening":
        score += 5.0

    # ⓪ SNS人気度（最優先）
    pop_score = float(spot.get("popularity_score") or 0)
    ig_score  = float(spot.get("instagram_score") or 0)
    tt_score  = float(spot.get("tiktok_score") or 0)
    theme_key = theme.get("key", "")

    if pop_score > 80:
        score += 4.5
    elif pop_score > 65:
        score += 3.0
    elif pop_score > 50:
        score += 1.5
    elif pop_score > 35:
        score += 0.5

    if theme_key == "photo" and ig_score > 70:
        score += 2.5
    elif theme_key in ("active", "gourmet", "classic") and tt_score > 70:
        score += 2.0
    elif theme_key == "local" and pop_score < 40:
        score += 2.0

    # ① スポット属性タグ × テーマ適合
    spot_tags = _get_spot_tags(spot)
    theme_tags: set[str] = theme.get("spot_tags") or set()
    if spot_tags & theme_tags:
        score += 3.0

    # ② テーマ prefer_types 適合
    if prefer & types:
        score += 2.5

    # ③ 天気連動スコア
    if "屋内" in weather_rec:
        if types & _INDOOR_TYPES:
            score += 2.5
        elif types & _OUTDOOR_TYPES:
            score -= 2.5
    elif "屋外" in weather_rec:
        if types & _OUTDOOR_TYPES:
            score += 2.0
        elif types & _INDOOR_TYPES:
            score -= 0.5

    # ④ SNSトレンド度（レビュー数）
    rc = int(spot.get("review_count") or 0)
    if rc > 5000:
        score += 2.0
    elif rc > 1000:
        score += 1.2
    elif rc > 300:
        score += 0.5
    elif rc < 50 and spot.get("source") != "gemini":
        score -= 0.5

    # ⑤ 高評価テーマ
    if theme.get("prefer_high_rating"):
        score += float(spot.get("rating") or 0) * 0.4

    # ⑥ 予算適合
    price = spot.get("price") or spot.get("price_label") or ""
    budget_cat_score = _budget_category(budget)
    if budget_cat_score == "low":
        if any(x in price for x in ("¥3,000", "¥5,000", "¥10,000", "¥15,000")):
            score -= 4.0
        elif price in ("無料", "〜¥500", "〜¥1,000"):
            score += 2.0
    elif budget_cat_score == "mid":
        if any(x in price for x in ("¥10,000", "¥15,000")):
            score -= 3.0
        elif price in ("無料", "〜¥500", "〜¥1,000"):
            score += 1.0
    elif budget_cat_score == "high":
        if any(x in price for x in ("¥5,000", "¥10,000", "¥15,000")):
            score += 1.5

    if theme.get("prefer_budget_low"):
        if price in ("無料", "〜¥500"):
            score += 2.0
        elif any(x in price for x in ("¥3,000", "¥1,500〜")):
            score -= 1.0

    # ⑦ 穴場テーマ
    if theme.get("prefer_low_review_count"):
        score += 2.0 if rc < 300 else (-0.5 if rc > 1000 else 0)

    # ⑧ 同行者タイプ適合（+3.0 prefer / −4.0 avoid）
    if people in _PEOPLE_PREFER and (_PEOPLE_PREFER[people] & types):
        score += 3.0
    if people in _PEOPLE_AVOID and (_PEOPLE_AVOID[people] & types):
        score -= 4.0

    # ⑨ 気分（purpose）適合 — 複数選択対応: prefer +5.0 / avoid -5.0
    purposes = _parse_purposes(purpose)
    spot_tags_for_purpose = _get_spot_tags(spot)
    _applied_photo_bonus = False
    _applied_spa_bonus   = False

    for p in purposes:
        if p in _PURPOSE_PREFER and (_PURPOSE_PREFER[p] & types):
            score += 5.0
        if p in _PURPOSE_AVOID and (_PURPOSE_AVOID[p] & types):
            score -= 5.0

        # ⑨-b 気分ごとのタグ追加ボーナス
        if p == "自然":
            if "nature" in spot_tags_for_purpose or "scenic" in spot_tags_for_purpose:
                score += 3.5
            if types & {"shopping_mall", "store", "movie_theater", "night_club"}:
                score -= 6.0
        elif p == "グルメ":
            if "gourmet" in spot_tags_for_purpose:
                score += 3.5
            if types & {"natural_feature", "campground", "park"} and not (types & _MEAL_TYPES):
                score -= 3.0
        elif p in ("写真映え", "SNS映え") and not _applied_photo_bonus:
            _applied_photo_bonus = True
            if ig_score > 70:
                score += 3.5
            if "photo" in spot_tags_for_purpose:
                score += 2.0
        elif p in ("温泉", "ゆっくり") and not _applied_spa_bonus:
            _applied_spa_bonus = True
            if "spa" in types:
                score += 4.0
        elif p == "歴史":
            if "history" in spot_tags_for_purpose:
                score += 3.5
            if types & {"amusement_park", "gym", "shopping_mall"}:
                score -= 4.0
        elif p == "アクティブ":
            if "outdoor" in spot_tags_for_purpose:
                score += 3.5
            if types & {"museum", "art_gallery", "spa", "shopping_mall"}:
                score -= 4.0

    # ⑩ クロステーマ多様性（使用済みスポットに強いペナルティ）
    if used_names and spot.get("name") in used_names:
        score -= 25.0

    # ⑪ 現在地からの近接スコア（GPS取得時のみ有効）
    if user_lat and user_lon:
        score += _proximity_score(spot, user_lat, user_lon)

    # ⑫ 季節・気温適合スコア
    if season:
        score += _season_score(spot, season, temperature)

    # ⑬ 提案履歴ペナルティ（昨日以前に出たスポットは軽く減点）
    if history_names and spot.get("name") in history_names:
        score -= 3.0

    return score


# ── Geographic helpers ────────────────────────────────────────────────────────

_CLUSTER_RADIUS_KM  = 20.0   # エリア半径（旧25km→20km）
_MAX_HOP_MIN        = 45     # スポット間の最大移動時間（分）


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _travel_min_between(a: dict, b: dict) -> int:
    """
    Estimate travel time from a to b.
    Uses 30km/h (car/transit) — previous version used 15km/h (walking only).
    """
    lat1 = a.get("latitude") or 0.0
    lon1 = a.get("longitude") or 0.0
    lat2 = b.get("latitude") or 0.0
    lon2 = b.get("longitude") or 0.0
    if not (lat1 and lon1 and lat2 and lon2):
        return 15
    dist_km = _haversine(float(lat1), float(lon1), float(lat2), float(lon2))
    return max(5, min(60, int(dist_km / 30 * 60) + 5))


# ── Area clustering ───────────────────────────────────────────────────────────

def _find_all_clusters(
    spots: list[dict],
    min_size: int = 4,
    radius_km: float = _CLUSTER_RADIUS_KM,
    max_clusters: int = 3,
) -> list[list[dict]]:
    """
    Greedily find up to max_clusters geographic clusters.
    Each cluster is a dense group of spots within radius_km.
    Spots without coordinates go into the largest cluster.
    """
    coord    = [s for s in spots if s.get("latitude") and s.get("longitude")]
    no_coord = [s for s in spots if not (s.get("latitude") and s.get("longitude"))]

    if len(coord) < min_size:
        return [spots]

    remaining = list(coord)
    clusters:  list[list[dict]] = []

    while len(remaining) >= min_size and len(clusters) < max_clusters:
        best_center: dict | None = None
        best_members: list[dict] = []

        for anchor in remaining:
            members = [
                s for s in remaining
                if _haversine(
                    float(anchor["latitude"]), float(anchor["longitude"]),
                    float(s["latitude"]),      float(s["longitude"]),
                ) <= radius_km
            ]
            if len(members) > len(best_members):
                best_members = members
                best_center  = anchor

        if len(best_members) < min_size:
            break

        clusters.append(list(best_members))
        cluster_ids = {id(s) for s in best_members}
        remaining   = [s for s in remaining if id(s) not in cluster_ids]

    if not clusters:
        return [spots]

    # Attach no-coord spots to the largest cluster
    if no_coord:
        largest = max(range(len(clusters)), key=lambda i: len(clusters[i]))
        clusters[largest].extend(no_coord)

    # Assign leftover spots to nearest cluster centroid
    for s in remaining:
        lat, lng = float(s.get("latitude") or 0), float(s.get("longitude") or 0)
        best_i, best_d = 0, float("inf")
        for i, cl in enumerate(clusters):
            cl_lats = [float(x["latitude"])  for x in cl if x.get("latitude")]
            cl_lngs = [float(x["longitude"]) for x in cl if x.get("longitude")]
            if cl_lats:
                c_lat = sum(cl_lats) / len(cl_lats)
                c_lng = sum(cl_lngs) / len(cl_lngs)
                d = _haversine(lat, lng, c_lat, c_lng)
                if d < best_d:
                    best_d, best_i = d, i
        clusters[best_i].append(s)

    return [c for c in clusters if c]  # remove empty


def _cluster_area_name(spots: list[dict]) -> str:
    """Extract the most common city/town name from cluster spot addresses."""
    names: list[str] = []
    for s in spots:
        addr = s.get("address", "")
        m = re.search(r"([^\s県都道府]{2,5}[市区町村])", addr)
        if m:
            names.append(m.group(1))
    if not names:
        return ""
    return Counter(names).most_common(1)[0][0]


def _weather_category(weather: dict) -> str:
    """Return 'sunny', 'cloudy', or 'rainy' from weather dict."""
    condition = weather.get("condition", "")
    rec       = weather.get("recommendation", "")
    try:
        rain_pct = int(weather.get("rain_chance", 0))
    except (ValueError, TypeError):
        rain_pct = 0
    if "雨" in condition or "雷" in condition or rain_pct >= 60 or "屋内中心" in rec:
        return "rainy"
    if rain_pct >= 30 or "くもり" in condition or "霧" in condition or "屋内と屋外" in rec:
        return "cloudy"
    return "sunny"


def _budget_category(budget: str) -> str:
    """Return 'low', 'mid', or 'high' from budget string."""
    if budget in ("1,000円以内", "3,000円以内"):
        return "low"
    if budget in ("5,000円以内", "10,000円以内"):
        return "mid"
    return "high"  # 15,000円以内 / 20,000円以内 / 30,000円以内 / 気にしない


def _parse_purposes(purpose: str) -> list[str]:
    """Split comma-separated purpose string into list of valid purposes."""
    if not purpose:
        return []
    return [p.strip() for p in purpose.split(",") if p.strip()]


# ── Season helpers ────────────────────────────────────────────────────────────

def _get_season() -> str:
    month = datetime.now().month
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "fall"
    return "winter"


# ── Spot-name seasonal keyword → season mapping ──────────────────────────────
# キーワードが含まれるスポット名は、そのシーズンのみ推奨される
_SPOT_SEASON_KEYWORDS: dict[str, str] = {
    # 春 (spring 3-5月)
    "桜": "spring", "さくら": "spring", "サクラ": "spring",
    "花見": "spring", "梅": "spring", "チューリップ": "spring",
    "藤": "spring", "菜の花": "spring", "新緑": "spring", "春まつり": "spring",
    # 夏 (summer 6-8月)
    "紫陽花": "summer", "あじさい": "summer", "アジサイ": "summer",
    "花菖蒲": "summer", "ホタル": "summer", "蛍": "summer",
    "ひまわり": "summer", "向日葵": "summer", "海水浴": "summer",
    "花火": "summer", "朝顔": "summer", "夏祭り": "summer",
    # 秋 (fall 9-11月)
    "紅葉": "fall", "もみじ": "fall", "モミジ": "fall",
    "銀杏": "fall", "いちょう": "fall", "イチョウ": "fall",
    "コスモス": "fall", "秋桜": "fall",
    # 冬 (winter 12-2月)
    "イルミネーション": "winter", "スキー": "winter",
    "スノー": "winter", "クリスマス": "winter", "初詣": "winter",
}

# 隣接シーズン（+10 ボーナス対象）
_ADJACENT_SEASONS: dict[str, frozenset[str]] = {
    "spring": frozenset({"winter", "summer"}),
    "summer": frozenset({"spring", "fall"}),
    "fall":   frozenset({"summer", "winter"}),
    "winter": frozenset({"fall", "spring"}),
}

# 月ごとのイベントキーワード（+20 ボーナス対象）
_MONTH_EVENT_KEYWORDS: dict[int, list[str]] = {
    1:  ["初詣", "お正月"],
    2:  ["梅", "バレンタイン"],
    3:  ["梅", "桜", "さくら"],
    4:  ["桜", "さくら", "花見", "チューリップ"],
    5:  ["藤", "新緑", "バラ"],
    6:  ["紫陽花", "あじさい", "花菖蒲", "ホタル", "蛍"],
    7:  ["ひまわり", "向日葵", "花火", "海"],
    8:  ["ひまわり", "向日葵", "花火", "海水浴"],
    9:  ["コスモス", "秋桜"],
    10: ["紅葉", "もみじ", "コスモス"],
    11: ["紅葉", "もみじ", "銀杏", "いちょう", "イチョウ"],
    12: ["イルミネーション", "クリスマス"],
}

# type ベースのフォールバック（季節キーワードがない汎用スポット用）
_SEASON_PREFER: dict[str, set[str]] = {
    "spring": {"park", "natural_feature", "cafe", "tourist_attraction", "campground", "beach"},
    "summer": {"aquarium", "beach", "amusement_park", "spa", "movie_theater"},
    "fall":   {"park", "natural_feature", "museum", "restaurant", "spa", "cafe"},
    "winter": {"spa", "shopping_mall", "museum", "art_gallery", "movie_theater", "restaurant"},
}
_SEASON_TYPE_AVOID: dict[str, set[str]] = {
    "winter": {"beach", "campground"},
    "summer": set(),
    "spring": set(),
    "fall":   set(),
}


def _season_score(spot: dict, season: str, temperature: float) -> float:
    """
    Keyword-first seasonal scoring.

    1. スポット名・住所から季節キーワードを検出
       - ベストシーズン一致 : +30
       - 隣接シーズン       : +10
       - 季節外             : -50  ← 実質除外
    2. 現在月のイベントボーナス : +20
    3. キーワードなし → type ベースのフォールバック (+1.5〜+3.5)
    4. 気温補正（既存ロジック）
    """
    if not season:
        return 0.0

    month  = datetime.now().month
    types  = set(spot.get("types") or [])
    label  = (spot.get("name") or "") + " " + (spot.get("address") or "")
    score  = 0.0

    # ── 1. 名前ベース季節判定 ──────────────────────────────────────────────
    found_season: str | None = None
    for kw, kw_season in _SPOT_SEASON_KEYWORDS.items():
        if kw in label:
            found_season = kw_season
            break  # 最初にマッチしたキーワードを採用

    if found_season is not None:
        if found_season == season:
            score += 30.0
        elif found_season in _ADJACENT_SEASONS.get(season, frozenset()):
            score += 10.0
        else:
            score -= 50.0   # 季節外：事実上除外
    else:
        # ── キーワードなし → type ベースフォールバック ──────────────────
        prefer = _SEASON_PREFER.get(season, set())
        avoid  = _SEASON_TYPE_AVOID.get(season, set())
        if prefer & types:
            score += 1.5
        if avoid & types:
            score -= 2.0
        # super bonus (元の _SEASON_SUPER_BONUS 相当)
        _super = {
            "spring": {"park", "natural_feature"},
            "summer": {"aquarium", "beach"},
            "fall":   {"park", "natural_feature"},
            "winter": {"spa"},
        }
        if _super.get(season, set()) & types:
            score += 2.0

    # ── 2. 月イベントボーナス ─────────────────────────────────────────────
    for kw in _MONTH_EVENT_KEYWORDS.get(month, []):
        if kw in label:
            score += 20.0
            break  # 1回だけ

    # ── 3. 気温補正（既存ロジック） ──────────────────────────────────────
    if temperature >= 35:
        if types & _OUTDOOR_TYPES:
            score -= 4.0
        if types & _INDOOR_TYPES:
            score += 2.0
    elif temperature >= 30:
        if types & (_OUTDOOR_TYPES - {"beach"}):
            score -= 2.0
        if types & {"cafe", "aquarium", "museum", "art_gallery", "shopping_mall"}:
            score += 1.0
    elif temperature <= 5:
        if types & {"park", "beach", "natural_feature", "campground"}:
            score -= 3.0
        if types & {"spa", "shopping_mall", "museum", "art_gallery"}:
            score += 2.0
    elif temperature <= 10:
        if types & {"spa", "museum", "art_gallery", "cafe"}:
            score += 1.0

    return score


def _is_off_season_spot(spot: dict, season: str) -> bool:
    """スポット名に季節外キーワードが含まれる場合 True を返す。"""
    label = (spot.get("name") or "") + " " + (spot.get("address") or "")
    for kw, kw_season in _SPOT_SEASON_KEYWORDS.items():
        if kw in label:
            if kw_season != season and kw_season not in _ADJACENT_SEASONS.get(season, frozenset()):
                return True
    return False


def _match_spot_weather(start_time: str, hourly: list[dict]) -> dict | None:
    """Find the hourly weather slot closest to a spot's start_time."""
    if not hourly:
        return None
    try:
        h, m     = map(int, start_time.split(":"))
        spot_min = h * 60 + m
        return min(
            hourly,
            key=lambda s: abs(
                int(s.get("time", "12:00").split(":")[0]) * 60
                + int(s.get("time", "12:00").split(":")[1])
                - spot_min
            ),
        )
    except Exception:
        return None


def _select_themes_dynamic(
    weather: dict,
    people: str,
    budget: str,
    purpose: str = "",
    max_themes: int = 6,
) -> list[dict]:
    """
    Dynamically select up to max_themes from ALL_THEMES based on current
    conditions (weather / people / budget).

    Each theme is scored by how well it matches:
      +3 weather match, +3 people match, +2 budget match, +1.5 purpose match
    Themes that *exclude* the current conditions are dropped entirely.
    """
    weather_cat = _weather_category(weather)
    budget_cat  = _budget_category(budget)
    purposes    = _parse_purposes(purpose)

    _PURPOSE_THEME_AFFINITY: dict[str, set[str]] = {
        "ゆっくり":   {"relax", "onsen", "cafe_hopping"},
        "SNS映え":    {"photo", "scenic", "date"},
        "自然":       {"scenic", "drive", "active", "walking", "free_spots"},
        "デート":     {"date", "night_view", "cafe_hopping"},
        "食べ歩き":   {"gourmet", "cafe_hopping", "local"},
        "グルメ":     {"gourmet", "cafe_hopping", "local"},
        "写真映え":   {"photo", "scenic", "date", "night_view"},
        "温泉":       {"onsen", "relax", "cafe_hopping"},
        "歴史":       {"classic", "local", "walking", "photo"},
        "アクティブ": {"active", "drive", "scenic", "experience"},
    }
    purpose_keys: set[str] = set()
    for p in purposes:
        purpose_keys |= _PURPOSE_THEME_AFFINITY.get(p, set())

    # Build union whitelist from all selected purposes
    whitelist: set[str] | None = None
    if purposes:
        whitelist = set()
        for p in purposes:
            wl = _PURPOSE_THEME_WHITELIST.get(p)
            if wl:
                whitelist |= wl
        if not whitelist:
            whitelist = None

    scored: list[tuple[dict, float]] = []

    for theme in ALL_THEMES:
        weather_ok = theme.get("weather_ok") or set()
        people_ok  = theme.get("people_ok")  or set()
        budget_ok  = theme.get("budget_ok")  or set()

        if weather_ok and weather_cat not in weather_ok:
            continue
        if people_ok and people not in people_ok:
            continue
        if budget_ok and budget_cat not in budget_ok:
            continue

        priority = float(theme.get("base_priority", 5))

        if weather_ok and weather_cat in weather_ok:
            priority += 3.0
        if people_ok and people in people_ok:
            priority += 3.0
        if budget_ok and budget_cat in budget_ok:
            priority += 2.0
        if theme.get("key") in purpose_keys:
            priority += 1.5

        scored.append((theme, priority))

    # Purpose whitelist → scoring BOOST (+4) rather than hard filter.
    # Hard filtering caused <10 themes when purpose was set (e.g. "グルメ" → only 5).
    if whitelist:
        effective_wl = whitelist | (_RAINY_FALLBACK_THEMES if weather_cat == "rainy" else set())
        scored = [
            (t, p + 4.0) if t.get("key") in effective_wl else (t, p)
            for t, p in scored
        ]

    scored.sort(key=lambda x: x[1], reverse=True)

    # Take only themes with a quality threshold: top-scoring AND not below base priority
    # This prevents off-theme filler being added just to hit a count target.
    MIN_PRIORITY = 5.0
    qualified = [(t, p) for t, p in scored if p >= MIN_PRIORITY]

    # Fall back to all scored if nothing qualifies
    pool = qualified if qualified else scored
    selected = [t for t, _ in pool[:max_themes]]

    # Always guarantee at least 3 plans
    if len(selected) < 3:
        for t, _ in scored:
            if t not in selected:
                selected.append(t)
            if len(selected) >= 3:
                break

    return selected


def _assign_cluster_themes(
    selected_themes: list[dict],
    cluster_idx: int,
    n_clusters: int,
) -> list[dict]:
    """
    Distribute pre-selected themes across clusters via round-robin.
    With 6 themes and 2 clusters:
      cluster 0 → themes[0, 2, 4]
      cluster 1 → themes[1, 3, 5]
    """
    assigned = [t for i, t in enumerate(selected_themes) if i % max(n_clusters, 1) == cluster_idx]
    if len(assigned) < 2:
        # Pad to at least 2 so each cluster can generate multiple courses
        for t in selected_themes:
            if t not in assigned:
                assigned.append(t)
            if len(assigned) >= 2:
                break
    return assigned


# ── Filter / constraint helpers ───────────────────────────────────────────────

_BREAKFAST_MIN   = 7 * 60
_BREAKFAST_MAX   = 9 * 60 + 30
_LUNCH_MIN       = 11 * 60 + 30
_LUNCH_MAX       = 14 * 60
_SNACK_MIN       = 14 * 60 + 30
_SNACK_MAX       = 16 * 60
_DINNER_MIN      = 17 * 60
_DINNER_MAX      = 20 * 60
_MEAL_TYPES      = frozenset({"restaurant", "food", "meal_delivery", "meal_takeaway", "bar"})
_BREAKFAST_TYPES = frozenset({"bakery", "cafe"})
_SNACK_TYPES     = frozenset({"cafe", "bakery"})

_CHAIN_NAMES: frozenset[str] = frozenset({
    "スターバックス", "スタバ", "starbucks",
    "マクドナルド", "モスバーガー", "ドムドムバーガー", "ロッテリア",
    "コメダ珈琲", "コメダ", "ドトール", "タリーズ", "サンマルク",
    "吉野家", "松屋", "すき家", "なか卯",
    "ガスト", "デニーズ", "ジョナサン", "サイゼリヤ", "バーミヤン",
    "ジョリーパスタ", "ビッグボーイ",
})


def _is_chain(name: str) -> bool:
    n = name.lower()
    return any(c.lower() in n for c in _CHAIN_NAMES)


_CATEGORY_TYPES: dict[str, set[str]] = {
    "nature":      {"park", "natural_feature", "beach", "campground"},
    "cafe":        {"cafe", "bakery"},
    "food":        {"restaurant", "food", "meal_delivery", "meal_takeaway", "bar"},
    "experience":  {"amusement_park", "zoo", "aquarium", "spa", "gym", "movie_theater"},
    "sightseeing": {"museum", "art_gallery", "tourist_attraction", "shrine", "temple",
                    "point_of_interest"},
    "shopping":    {"shopping_mall", "store"},
}

_CATEGORY_LABELS_JA: dict[str, str] = {
    "nature":      "自然",
    "cafe":        "カフェ",
    "food":        "食事",
    "experience":  "体験",
    "sightseeing": "観光名所",
    "shopping":    "ショッピング",
    "other":       "スポット",
}


def _category_of(spot: dict) -> str:
    types = set(spot.get("types") or [])
    for cat, cat_types in _CATEGORY_TYPES.items():
        if types & cat_types:
            return cat
    return "other"


def _select(
    theme: dict,
    cluster_spots: list[dict],
    people: str,
    n: int,
    weather_rec: str = "",
    purpose: str = "",
    budget: str = "",
    used_names: set[str] | None = None,
    user_lat: float = 0.0,
    user_lon: float = 0.0,
    season: str = "",
    temperature: float = 20.0,
    history_names: frozenset[str] = frozenset(),
    penalty_map: dict | None = None,
    trend_data: dict | None = None,
) -> list[dict]:
    # ① 季節外スポットをハード除外（-50 ペナルティでは不十分なケースに備えて）
    if season:
        in_season = [s for s in cluster_spots if not _is_off_season_spot(s, season)]
        if len(in_season) >= n:
            cluster_spots = in_season
        elif in_season:
            cluster_spots = in_season  # 数が少なくても季節外は外す

    # ② 営業時間フィルタ
    open_spots = [s for s in cluster_spots if s.get("open_now") is not False]
    pool = open_spots if open_spots else cluster_spots

    # ③ チェーン店フィルタ
    non_chain = [s for s in pool if not _is_chain(s["name"])]
    pool = non_chain if non_chain else pool

    # ④ 同行者ハードフィルタ（回避タイプを除外）
    avoid = _PEOPLE_AVOID.get(people, set())
    if avoid:
        filtered = [s for s in pool if not (set(s.get("types") or []) & avoid)]
        if len(filtered) >= n:
            pool = filtered

    # ⑤ 予算ハードフィルタ
    if _budget_category(budget) == "low":
        affordable = [
            s for s in pool
            if not any(
                x in (s.get("price") or s.get("price_label") or "")
                for x in ("¥3,000", "¥5,000", "¥10,000", "¥15,000")
            )
        ]
        if len(affordable) >= n:
            pool = affordable

    # ⑥ スコアリング（近接スコア込み）
    scored = sorted(
        pool,
        key=lambda s: _score(
            s, theme, people, weather_rec, purpose, budget, used_names,
            user_lat, user_lon, season, temperature, history_names,
            penalty_map, trend_data,
        ),
        reverse=True,
    )
    candidates = scored[:max(n * 3, 12)]

    # ⑦ 移動時間フィルタ（クラスタ内でも離れすぎたスポットを除外）
    if len(candidates) > n:
        anchor = candidates[0]
        reachable = [
            s for s in candidates
            if not (s.get("latitude") and s.get("longitude"))
            or _travel_min_between(anchor, s) <= _MAX_HOP_MIN
        ]
        if len(reachable) >= n:
            candidates = reachable

    return candidates[:n]


def _limit_famous_spots(spots: list[dict], max_count: int = 1) -> list[dict]:
    """定番観光地 (tourist_attraction) は1プランにmax_count件まで。超過分を除外する。"""
    LANDMARK_TYPE = "tourist_attraction"
    result: list[dict] = []
    cnt = 0
    for spot in spots:
        types = set(spot.get("types") or [])
        if LANDMARK_TYPE in types:
            if cnt >= max_count:
                continue  # 超過分をスキップ
            cnt += 1
        result.append(spot)
    return result if len(result) >= max(2, len(spots) - 1) else spots


def _ensure_surprise_slots(
    spots: list[dict],
    cluster_spots: list[dict],
    n: int,
    used_names: set[str],
) -> list[dict]:
    """
    プランの約20%（最低1件）をサプライズ枠（穴場・SNS急上昇・新規オープン・ローカル人気）にする。
    対象スポットが既に含まれていれば何もしない。
    """
    SURPRISE_TREND_TYPES = {"new_opening", "local_hidden", "trending", "event"}
    SURPRISE_TAGS        = {"hidden_gem"}

    target = max(1, round(n * 0.20))

    def _is_surprise(s: dict) -> bool:
        if s.get("trend_type") in SURPRISE_TREND_TYPES:
            return True
        if SURPRISE_TAGS & _get_spot_tags(s):
            return True
        return False

    current_surprise = sum(1 for s in spots if _is_surprise(s))
    if current_surprise >= target:
        return spots  # 既に十分

    plan_names = {s["name"] for s in spots}
    candidates = [
        s for s in cluster_spots
        if _is_surprise(s)
        and s["name"] not in used_names
        and s["name"] not in plan_names
    ]
    if not candidates:
        return spots

    # 評価の高いサプライズ候補を優先
    candidates.sort(key=lambda s: float(s.get("rating") or 0), reverse=True)

    result = list(spots)
    added  = 0
    for cand in candidates:
        if added >= (target - current_surprise):
            break
        # 最もスコアの低い非必須スポットと交換
        replaceable = [
            (i, s) for i, s in enumerate(result)
            if not (set(s.get("types") or []) & _MEAL_TYPES)
            and not _is_surprise(s)
        ]
        if not replaceable:
            break
        # スコアで最下位の非食事スポットと交換
        idx, _ = min(replaceable, key=lambda x: float(x[1].get("rating") or 0))
        result[idx] = cand
        used_names.add(cand["name"])
        added += 1
        logger.info("[Surprise] サプライズ枠追加: %s (trend=%s)", cand["name"], cand.get("trend_type"))

    return result


def _filter_type_bias(spots: list[dict], n: int, max_same: int = 2) -> list[dict]:
    RESTRICTED = {
        "tourist_attraction", "natural_feature", "park",
        "museum", "art_gallery", "shrine", "temple",
    }
    type_count: dict[str, int] = {}
    result: list[dict] = []
    for spot in spots:
        primary = next((t for t in (spot.get("types") or []) if t in RESTRICTED), None)
        if primary:
            cnt = type_count.get(primary, 0)
            if cnt >= max_same:
                continue
            type_count[primary] = cnt + 1
        result.append(spot)
    return result if len(result) >= n else spots


def _enforce_meals(
    spots: list[dict],
    pool: list[dict],
    start_time: str,
    end_time: str,
    theme: dict,
    people: str,
    weather_rec: str,
    purpose: str = "",
    budget: str = "",
    used_names: set[str] | None = None,
    user_lat: float = 0.0,
    user_lon: float = 0.0,
    season: str = "",
    temperature: float = 20.0,
    history_names: frozenset[str] = frozenset(),
) -> list[dict]:
    start_min = to_min(start_time)
    end_min   = to_min(end_time)
    n         = len(spots)
    if n == 0:
        return spots

    result = list(spots)
    used   = {s["name"] for s in result}

    def _sc(s: dict) -> float:
        return _score(
            s, theme, people, weather_rec, purpose, budget, used_names,
            user_lat, user_lon, season, temperature, history_names,
        )

    def _target_idx(meal_min: int) -> int:
        span     = max(end_min - start_min, 1)
        progress = max(0.0, min(1.0, (meal_min - start_min) / span))
        return max(1, min(len(result) - 1, round(progress * len(result))))

    def _types_present_near(idx: int, types_set: frozenset, window: int = 1) -> bool:
        lo = max(0, idx - window)
        hi = min(len(result), idx + window + 1)
        return any(set(result[i].get("types", [])) & types_set for i in range(lo, hi))

    def _best_of_type(excl: set[str], types_set: frozenset) -> dict | None:
        cands = sorted(
            [s for s in pool if set(s.get("types", [])) & types_set and s["name"] not in excl],
            key=_sc, reverse=True,
        )
        return cands[0] if cands else None

    def _insert_at(meal: dict, t_idx: int, keep_types: frozenset) -> None:
        used.add(meal["name"])
        if t_idx < len(result) and not (set(result[t_idx].get("types", [])) & keep_types):
            result[t_idx] = meal
        else:
            result.insert(min(t_idx, len(result)), meal)
            if len(result) > n:
                removable = [
                    i for i, s in enumerate(result)
                    if not (set(s.get("types", [])) & keep_types) and i != t_idx
                ]
                if removable:
                    result.pop(min(removable, key=lambda i: _sc(result[i])))

    _ALL_FOOD = _MEAL_TYPES | _BREAKFAST_TYPES

    # ── Breakfast (7:00–9:30) — insert at front ───────────────────────────────
    if start_min <= _BREAKFAST_MAX and end_min >= _BREAKFAST_MIN + 60:
        if not _types_present_near(0, _BREAKFAST_TYPES, window=0):
            bk = _best_of_type(used, _BREAKFAST_TYPES)
            if bk:
                used.add(bk["name"])
                result.insert(0, bk)
                if len(result) > n:
                    removable = [
                        i for i in range(1, len(result))
                        if not (set(result[i].get("types", [])) & _ALL_FOOD)
                    ]
                    if removable:
                        result.pop(min(removable, key=lambda i: _sc(result[i])))

    # ── Lunch (11:30–14:00) ───────────────────────────────────────────────────
    if start_min <= _LUNCH_MIN and end_min >= _LUNCH_MIN + 60:
        lunch_idx = _target_idx(_LUNCH_MIN)
        if not _types_present_near(lunch_idx, _MEAL_TYPES):
            meal = _best_of_type(used, _MEAL_TYPES)
            if meal:
                _insert_at(meal, lunch_idx, _MEAL_TYPES)

    # ── Afternoon snack (14:30–16:00) ─────────────────────────────────────────
    if start_min <= _SNACK_MAX and end_min >= _SNACK_MIN + 60:
        snack_idx = _target_idx(_SNACK_MIN + 30)
        if not _types_present_near(snack_idx, _SNACK_TYPES):
            snack = _best_of_type(used, _SNACK_TYPES)
            if snack:
                used.add(snack["name"])
                result.insert(min(snack_idx, len(result)), snack)
                if len(result) > n:
                    removable = [
                        i for i, s in enumerate(result)
                        if not (set(s.get("types", [])) & _ALL_FOOD) and i != snack_idx
                    ]
                    if removable:
                        result.pop(min(removable, key=lambda i: _sc(result[i])))

    # ── Dinner (17:00–20:00) ──────────────────────────────────────────────────
    if start_min <= _DINNER_MIN and end_min >= _DINNER_MIN + 60:
        dinner_idx = _target_idx(_DINNER_MIN)
        if not _types_present_near(dinner_idx, _MEAL_TYPES):
            meal = _best_of_type(used, _MEAL_TYPES)
            if meal:
                _insert_at(meal, dinner_idx, _MEAL_TYPES)

    return result


def _fix_consecutive(spots: list[dict], max_run: int = 2) -> list[dict]:
    if len(spots) <= max_run:
        return spots
    result = list(spots)
    i = max_run
    while i < len(result):
        cat_i = _category_of(result[i])
        run   = 1
        for j in range(i - 1, max(i - max_run - 1, -1), -1):
            if _category_of(result[j]) == cat_i:
                run += 1
            else:
                break
        if run > max_run:
            swap_at = next(
                (k for k in range(i + 1, len(result)) if _category_of(result[k]) != cat_i),
                None,
            )
            if swap_at is not None:
                result[i], result[swap_at] = result[swap_at], result[i]
        i += 1
    return result


def _optimize_route(spots: list[dict]) -> list[dict]:
    if len(spots) <= 2:
        return spots
    if not any(s.get("latitude") and s.get("longitude") for s in spots):
        return spots
    unvisited = list(spots)
    route = [unvisited.pop(0)]
    while unvisited:
        current = route[-1]
        nearest = min(
            unvisited,
            key=lambda s: _haversine(
                float(current.get("latitude") or 0), float(current.get("longitude") or 0),
                float(s.get("latitude") or 0),        float(s.get("longitude") or 0),
            ),
        )
        route.append(nearest)
        unvisited.remove(nearest)
    return route


def _ensure_diversity(
    selected: list[dict],
    pool: list[dict],
    n: int,
    theme: dict,
    people: str,
    weather_rec: str,
    purpose: str = "",
    budget: str = "",
    used_names: set[str] | None = None,
    user_lat: float = 0.0,
    user_lon: float = 0.0,
    season: str = "",
    temperature: float = 20.0,
    history_names: frozenset[str] = frozenset(),
) -> list[dict]:
    REQUIRED = {"food", "cafe"}
    present  = {_category_of(s) for s in selected}
    missing  = REQUIRED - present
    if not missing:
        return selected

    selected_names = {s["name"] for s in selected}
    result = list(selected)

    def _sc(s: dict) -> float:
        return _score(
            s, theme, people, weather_rec, purpose, budget, used_names,
            user_lat, user_lon, season, temperature, history_names,
        )

    for cat in missing:
        candidates = [
            s for s in pool
            if _category_of(s) == cat and s["name"] not in selected_names
        ]
        if not candidates:
            continue
        best = max(candidates, key=_sc)
        replaceable = [s for s in result if _category_of(s) not in REQUIRED]
        if replaceable:
            result.remove(min(replaceable, key=_sc))
        result.append(best)
        selected_names.add(best["name"])

    return result[:n]


def _people_flow_adjust(spots: list[dict], people: str) -> list[dict]:
    if not spots or people not in ("カップル", "家族"):
        return spots
    result = list(spots)
    n = len(result)
    if n < 3:
        return result

    if people == "カップル":
        EVENING_PREFER = {"viewpoint", "spa", "aquarium", "tourist_attraction"}
        half = n // 2
        for i in range(half):
            if set(result[i].get("types") or []) & EVENING_PREFER:
                for j in range(n - 1, half - 1, -1):
                    if not (set(result[j].get("types") or []) & EVENING_PREFER):
                        result[i], result[j] = result[j], result[i]
                        break

    elif people == "家族":
        MORNING_PREFER = {"amusement_park", "zoo", "aquarium", "park"}
        half = n // 2
        for i in range(half, n):
            if set(result[i].get("types") or []) & MORNING_PREFER:
                result.insert(0, result.pop(i))
                break

    return result


# ── Cost estimation ───────────────────────────────────────────────────────────

_TYPE_COST_ESTIMATE: dict[str, int] = {
    "cafe":              700,
    "bakery":            500,
    "restaurant":       1800,
    "food":             1500,
    "bar":              2000,
    "meal_delivery":    1200,
    "meal_takeaway":    1000,
    "museum":            800,
    "art_gallery":       600,
    "aquarium":         2000,
    "zoo":              1500,
    "amusement_park":   3000,
    "spa":              1500,
    "movie_theater":    1800,
    "shopping_mall":    2000,
    "gym":              1000,
    "park":                0,
    "natural_feature":     0,
    "beach":               0,
    "campground":        500,
    "tourist_attraction":500,
    "shrine":              0,
    "temple":            200,
    "church":              0,
}

_PRICE_LABEL_COST: dict[str, int] = {
    "無料":       0,
    "〜¥500":   500,
    "〜¥1,000": 1000,
    "〜¥2,000": 2000,
    "〜¥3,000": 3000,
    "〜¥5,000": 5000,
    "〜¥10,000": 10000,
    "¥10,000以上": 15000,
}


def _estimate_spot_cost(spot: dict) -> int:
    """Return estimated visit cost in yen for a single spot."""
    price = spot.get("price") or spot.get("price_label") or ""
    if price in _PRICE_LABEL_COST:
        return _PRICE_LABEL_COST[price]
    m = re.search(r"¥\s*(\d[\d,]*)", price)
    if m:
        return int(m.group(1).replace(",", ""))
    types = set(spot.get("types") or [])
    for t, cost in _TYPE_COST_ESTIMATE.items():
        if t in types:
            return cost
    return 300


# ── Reason generator ─────────────────────────────────────────────────────────

def _build_reason(
    theme: dict,
    weather: dict,
    people: str = "",
    budget: str = "",
    area_name: str = "",
    purpose: str = "",
) -> str:
    """
    Generate a one-sentence reason explaining why this specific course was
    proposed, based on the current conditions (weather/people/budget/purpose).
    Priority: weather-specific > people-specific > budget-specific > purpose > default.
    """
    key         = theme.get("key", "")
    weather_cat = _weather_category(weather)
    budget_cat  = _budget_category(budget)
    purposes    = _parse_purposes(purpose)

    # 1. Weather-specific reason (most informative when weather drove the selection)
    reason = _REASON_BY_WEATHER.get((weather_cat, key))
    if reason:
        suffix = f"（{area_name}エリア中心）" if area_name else ""
        return reason.rstrip("。") + suffix + "。"

    # 2. People-specific reason
    reason = _REASON_BY_PEOPLE.get((people, key))
    if reason:
        return reason

    # 3. Budget-specific reason
    if budget_cat == "low" and key == "budget":
        label = "1,000円以内" if "1,000" in budget else "3,000円以内"
        return f"予算{label}のため、コスパ重視でリーズナブルに楽しめるスポットを選びました。"
    if budget_cat == "low" and key == "free_spots":
        return "予算を抑えるため、無料・低コストスポットを中心に巡るコースを提案しました。"
    if budget_cat in ("mid", "high") and key in ("classic", "gourmet"):
        return theme.get("reason", theme.get("description", ""))

    # 4. Multiple purpose explanation
    if len(purposes) >= 2:
        labels = "・".join(purposes[:3])
        base   = theme.get("reason", theme.get("description", ""))
        suffix = f"（{area_name}エリア中心）" if area_name else ""
        return f"{labels}を楽しみたいあなたに、{base.rstrip('。')}{suffix}。"

    # 5. Fallback — theme's own reason field
    base = theme.get("reason", theme.get("description", ""))
    if area_name:
        base = base.rstrip("。") + f"（{area_name}エリア中心）。"
    return base


# ── Misc helpers ──────────────────────────────────────────────────────────────

def _plan_id(title: str, area: str) -> str:
    return sha1(f"{area}:{title}".encode()).hexdigest()[:12]


def _links(name: str, address: str, place_id: str = "") -> dict[str, str]:
    enc  = quote_plus(name)
    addr = quote_plus(address or name)
    ig   = quote(name.replace(" ", "").replace("　", ""), safe="")
    d = {
        "map":        f"https://www.google.com/maps/search/?api=1&query={enc}",
        "directions": f"https://www.google.com/maps/dir/?api=1&destination={addr}",
        "hotpepper":  f"https://www.hotpepper.jp/CSP/psh010/doBasic?keyword={enc}",
        "instagram":  f"https://www.instagram.com/explore/tags/{ig}/",
        "tiktok":     f"https://www.tiktok.com/search?q={enc}",
        "official":   "",
    }
    if place_id:
        d["map"]        = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
        d["directions"] = f"https://www.google.com/maps/dir/?api=1&destination=place_id:{place_id}"
    return d


# ── Main builder ──────────────────────────────────────────────────────────────

def _overlap_rate(plan_a: dict, plan_b: dict) -> float:
    """Fraction of spots shared between two plans (relative to the smaller plan)."""
    names_a = frozenset(s["name"] for s in plan_a.get("spots", []) if s.get("name"))
    names_b = frozenset(s["name"] for s in plan_b.get("spots", []) if s.get("name"))
    smaller = min(len(names_a), len(names_b))
    if smaller == 0:
        return 0.0
    return len(names_a & names_b) / smaller


def _filter_by_overlap(plans: list[dict], max_rate: float = 0.30) -> list[dict]:
    """
    Greedily keep plans whose spot overlap with every already-accepted plan
    stays below max_rate. Plans are processed in input order (highest-quality
    themes come first from _select_themes_dynamic).

    Result: 3–10 diverse, high-quality plans.
    """
    accepted: list[dict] = []
    for plan in plans:
        worst = max(
            (_overlap_rate(plan, acc) for acc in accepted),
            default=0.0,
        )
        if worst < max_rate:
            accepted.append(plan)
        else:
            logger.info(
                "[Overlap] '%s' 除外 (最大重複率=%.0f%%)",
                plan.get("plan_title", "?"), worst * 100,
            )
    return accepted


def build_all_plans(
    places: list[dict[str, Any]],
    search: dict[str, str],
    weather: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Build 10 themed 1-day plans (4–6 spots each) from Places spots.

    Architecture:
      1. Select 10 themes dynamically based on weather/people/budget conditions
      2. Cluster spots geographically (20km radius, up to 3 areas)
      3. Distribute selected themes across clusters via round-robin
      4. Within each cluster, build plans using only that area's spots
    """
    if not places:
        return []

    area        = search.get("place", "")
    start_time  = search.get("start_time", "10:00")
    end_time    = search.get("end_time",   "17:00")
    budget      = search.get("budget",     "気にしない")
    people      = search.get("people",     "")
    purpose     = search.get("purpose",    "")
    weather_rec = (weather or {}).get("recommendation", "") if weather else ""

    # GPS座標（取得できた場合のみ有効; スポット近接スコアリングに使用）
    try:
        user_lat = float(search.get("user_lat") or "")
        user_lon = float(search.get("user_lon") or "")
    except (ValueError, TypeError):
        user_lat = user_lon = 0.0

    if user_lat and user_lon:
        logger.info("[PlanBuilder] GPS座標あり: lat=%.4f lon=%.4f → 近接スコアリング有効",
                    user_lat, user_lon)

    # ── 季節・気温・提案履歴 ─────────────────────────────────────────────────
    season      = _get_season()
    try:
        temperature = float(weather.get("temperature") or 20.0)
    except (ValueError, TypeError):
        temperature = 20.0

    raw_history     = search.get("spot_history") or []
    history_names   = frozenset(raw_history) if isinstance(raw_history, list) else frozenset()
    penalty_map: dict = search.get("penalty_map") or {}
    trend_data:  dict = search.get("trend_data")  or {}
    logger.info(
        "[PlanBuilder] 季節=%s 気温=%.1f℃ 履歴=%d件 ペナルティ=%d件 トレンド=%d件",
        season, temperature, len(history_names), len(penalty_map), len(trend_data),
    )

    # ── hourly weather list for per-spot weather matching ────────────────────
    hourly_weather: list[dict] = weather.get("hourly") or []

    n_spots = ideal_spot_count(start_time, end_time)
    dur_h   = (to_min(end_time) - to_min(start_time)) // 60

    # ── Step 1: Dynamic theme selection based on conditions ───────────────────
    selected_themes = _select_themes_dynamic(
        weather or {}, people, budget, purpose, max_themes=10,
    )
    weather_cat = _weather_category(weather or {})
    budget_cat  = _budget_category(budget)
    logger.info(
        "[PlanBuilder] 動的テーマ選択: %d件 (天気=%s, 同行者=%s, 予算=%s)",
        len(selected_themes), weather_cat, people, budget_cat,
    )
    for t in selected_themes:
        logger.info("  → %s", t["title"])

    # ── Step 2: Cluster spots by geographic area ──────────────────────────────
    clusters    = _find_all_clusters(places, min_size=4, radius_km=_CLUSTER_RADIUS_KM)
    n_clusters  = len(clusters)
    logger.info("[PlanBuilder] %d エリアクラスタ検出 (合計%d件)", n_clusters, len(places))

    # トレンドデータをスポットへ付与（スコアリングで参照される）
    if trend_data:
        from services.trend_service import apply_trend_to_spots
        clusters = [apply_trend_to_spots(cl, trend_data) for cl in clusters]

    for i, cl in enumerate(clusters):
        area_name = _cluster_area_name(cl)
        logger.info("  クラスタ%d: %d件 エリア=%s", i, len(cl), area_name or "不明")

    result:      list[dict[str, Any]] = []
    used_global: set[str] = set()  # 全クラスタ通じた使用済みスポット（重複排除に使用）

    # ── Step 3: Build courses per cluster ────────────────────────────────────
    for cluster_idx, cluster_spots in enumerate(clusters):
        if len(cluster_spots) < 3:
            continue

        area_name  = _cluster_area_name(cluster_spots)
        themes     = _assign_cluster_themes(selected_themes, cluster_idx, n_clusters)

        # Spot count: target 4-6 per plan. n_spots is already in [4,6].
        adaptive_n = max(4, min(6, n_spots))

        logger.info("[PlanBuilder] クラスタ%d(%s): %dテーマ × %dスポット",
                    cluster_idx, area_name, len(themes), adaptive_n)

        for theme in themes:
            raw = _select(
                theme, cluster_spots, people, adaptive_n,
                weather_rec, purpose, budget, used_global, user_lat, user_lon,
                season, temperature, history_names,
                penalty_map, trend_data,
            )
            if not raw:
                continue

            raw = _ensure_diversity(
                raw, cluster_spots, adaptive_n, theme, people,
                weather_rec, purpose, budget, used_global, user_lat, user_lon,
                season, temperature, history_names,
            )
            raw = _filter_type_bias(raw, adaptive_n)
            raw = _enforce_meals(
                raw, cluster_spots, start_time, end_time, theme, people,
                weather_rec, purpose, budget, used_global, user_lat, user_lon,
                season, temperature, history_names,
            )
            raw = _optimize_route(raw)
            raw = _people_flow_adjust(raw, people)
            raw = _fix_consecutive(raw)
            raw = _limit_famous_spots(raw, max_count=1)
            raw = _ensure_surprise_slots(raw, cluster_spots, adaptive_n, used_global)

            travel_times = [
                _travel_min_between(raw[i], raw[i + 1])
                for i in range(len(raw) - 1)
            ]

            scheduled = schedule_spots(raw, start_time, end_time, travel_times=travel_times)
            if not scheduled:
                continue

            built: list[dict] = []
            for s in scheduled:
                name = s["name"]
                addr = s.get("address", area)
                pid  = s.get("place_id", "")
                built.append({
                    "name":             name,
                    "start_time":       s["start_time"],
                    "end_time":         s["end_time"],
                    "travel_min":       s["travel_min"],
                    "price":            s.get("price") or s.get("price_label") or "不明",
                    "rating":           float(s.get("rating") or 4.0),
                    "photo_url":        s.get("photo_url", ""),
                    "address":          addr,
                    "links":            _links(name, addr, pid),
                    "types":            s.get("types", []),
                    "source":           s.get("source", "places"),
                    "category_label":   _CATEGORY_LABELS_JA.get(_category_of(s), "スポット"),
                    "spot_tags":        list(_get_spot_tags(s)),
                    "latitude":         s.get("latitude"),
                    "longitude":        s.get("longitude"),
                    "popularity_score": s.get("popularity_score", 0),
                    "instagram_score":  s.get("instagram_score", 0),
                    "tiktok_score":     s.get("tiktok_score", 0),
                    "estimated_cost":   _estimate_spot_cost(s),
                    "is_indoor":        bool(set(s.get("types") or []) & _INDOOR_TYPES),
                    "spot_weather":     _match_spot_weather(s["start_time"], hourly_weather),
                    "trend_type":       s.get("trend_type", ""),
                    "trend_label":      s.get("trend_label", ""),
                    "trend_reason":     s.get("trend_reason", ""),
                    "place_id":         s.get("place_id", ""),
                })

            if not built:
                continue

            # 天気変化に合わせてプランを最適化
            built, weather_optimized = _optimize_for_weather(
                built, cluster_spots, hourly_weather, used_global,
            )

            # グローバル使用済みスポットを更新（次のテーマ・クラスタで重複回避）
            used_global.update(s["name"] for s in built)

            # 重複率をログ出力（デバッグ）
            overlap_ratio = 0.0

            first_photo  = next((s["photo_url"] for s in built if s["photo_url"]), "")
            avg_rating   = round(sum(s["rating"] for s in built) / len(built), 1)
            rain_ok      = bool(theme.get("prefer_indoor"))
            total_cost   = sum(s.get("estimated_cost", 0) for s in built)

            # タイトルにエリア名を含める
            plan_title = f"{area_name} {theme['title']}" if area_name else theme["title"]

            result.append({
                "id":               _plan_id(plan_title, area),
                "plan_title":       plan_title,
                "name":             plan_title,
                "spots":            built,
                "schedule_items":   [
                    {
                        "time":       s["start_time"],
                        "end_time":   s["end_time"],
                        "place":      s["name"],
                        "activity":   "訪問・観光",
                        "travel_min": s["travel_min"],
                    }
                    for s in built
                ],
                "start_time":       start_time,
                "end_time":         end_time,
                "total_spots":      len(built),
                "photo_url":        first_photo,
                "rating":           avg_rating,
                "type":             "1日コース",
                "category":         "indoor" if rain_ok else "mixed",
                "weather_tags":     ["sunny", "cloud", "rain"] if rain_ok else ["sunny", "cloud"],
                "rain_ok":          rain_ok,
                "area":             area_name or area,
                "address":          area_name or area,
                "access":           "",
                "hours":            f"{start_time}〜{end_time}",
                "budget":           budget,
                "price":            "スポットにより異なる",
                "stay_time":        f"{dur_h}時間",
                "highlight":        theme["description"],
                "description":      theme["description"],
                "trending_reason":  "",
                "audience":         "",
                "sns_appeal":       "",
                "sns_reason":       "",
                "recommended_time": "",
                "weather_match":    "",
                "tags":             theme["tags"],
                "links":            built[0]["links"],
                "estimated_total_cost": total_cost,
                "weather_optimized":    weather_optimized,
                "weather_comment":  _build_reason(
                    theme, weather or {}, people, budget, area_name, purpose,
                ),
            })

            logger.info("[PlanBuilder] '%s' → %d spots (クラスタ%d, %s〜%s)",
                        plan_title, len(built), cluster_idx, start_time, end_time)

    logger.info("[PlanBuilder] 合計 %d プラン生成 (%d クラスタ)", len(result), n_clusters)

    # ── 重複率フィルタ: 他プランと30%以上スポット被りのプランを除外 ──────────────
    filtered = _filter_by_overlap(result, max_rate=0.30)
    if len(filtered) < len(result):
        logger.info(
            "[PlanBuilder] 重複フィルタ: %d → %d プラン (重複率≥30%%を除外)",
            len(result), len(filtered),
        )
    return filtered

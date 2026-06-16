from __future__ import annotations

import concurrent.futures
import datetime
import json
import logging
import os
import random
import threading
import time
from hashlib import sha1
from typing import Any
from urllib.parse import quote, quote_plus

from services.image_service import photo_url_for_spot
from utils.helpers import calc_duration

logger = logging.getLogger(__name__)

# ── Gemini クライアント シングルトン ─────────────────────────────────────────
_gemini_client: Any = None
_gemini_client_key: str = ""


_GEMINI_HTTP_TIMEOUT = 5    # HTTP level timeout
_GEMINI_CALL_TIMEOUT = 4    # daemon thread join timeout per model attempt


def _get_or_create_gemini_client() -> tuple[Any, str]:
    """アプリ起動後に1回だけ初期化し、以降は同じインスタンスを返す。
    GOOGLE_API_KEY が設定されていても GEMINI_API_KEY を優先する。
    HTTP タイムアウトをクライアントレベルで設定する。
    """
    global _gemini_client, _gemini_client_key
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        logger.warning("[Gemini] GEMINI_API_KEY 未設定")
        return None, ""
    if _gemini_client is None or _gemini_client_key != key:
        from google import genai
        old_gkey = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            try:
                import httpx
                from google.genai import types as _ggt
                # httpx のデフォルトは初回接続が 5-6 秒かかる場合がある。
                # 明示的に設定した Client を注入することで接続タイムアウトを制御する。
                _hx = httpx.Client(
                    http2=False,
                    timeout=httpx.Timeout(
                        connect=3.0,
                        read=5.0,
                        write=3.0,
                        pool=3.0,
                    ),
                    verify=True,
                )
                _gemini_client = genai.Client(
                    api_key=key,
                    http_options=_ggt.HttpOptions(httpx_client=_hx),
                )
                logger.info("[Gemini] クライアント初期化 (httpx注入: connect=3s, read=5s)")
            except Exception as _e:
                logger.warning("[Gemini] カスタムhttpx初期化失敗(%s) → デフォルト", _e)
                _gemini_client = genai.Client(api_key=key)
        finally:
            if old_gkey is not None:
                os.environ["GOOGLE_API_KEY"] = old_gkey
        _gemini_client_key = key
        logger.info("[Gemini] クライアントシングルトン確定 (key=GEMINI_API_KEY)")
    return _gemini_client, key


def _call_gemini_timed(client: Any, gen_kwargs: dict) -> Any:
    """daemon スレッドで generate_content を呼び出す。
    _GEMINI_CALL_TIMEOUT 秒以内に完了しなければ TimeoutError を即raise し、
    バックグラウンドスレッドを放棄する（ブロックしない）。
    """
    holder: dict[str, Any] = {"response": None, "error": None}

    def _target() -> None:
        try:
            holder["response"] = client.models.generate_content(**gen_kwargs)
        except Exception as exc:
            holder["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=_GEMINI_CALL_TIMEOUT)
    if t.is_alive():
        # スレッドはバックグラウンドで動き続けるが、我々はここで放棄する
        model = gen_kwargs.get("model", "unknown")
        raise TimeoutError(f"[Gemini] {model} が {_GEMINI_CALL_TIMEOUT}s 以内に応答しなかった")
    if holder["error"] is not None:
        raise holder["error"]
    return holder["response"]


# ── Public API ────────────────────────────────────────────────────────────────

def suggest_fill_spots(
    area: str,
    people: str,
    purpose: str,
    n: int,
    exclude_names: list[str],
) -> list[dict[str, Any]]:
    """
    Places API のスポットが少ない場合、Gemini で補完スポットを提案する。
    返り値: Places スポットと同形式の dict リスト (source='gemini')
    """
    client, _ = _get_or_create_gemini_client()
    if client is None or n <= 0:
        return []

    model      = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    excl       = "、".join(exclude_names[:15]) if exclude_names else "なし"
    people_str = people or "観光客"

    prompt = f"""あなたは旅行プランナーAIです。
{area}で{people_str}が楽しめる実在スポットを{n}件提案してください。

【絶対ルール】
- 必ず{area}内の実在する施設名のみ（架空スポット禁止）
- Google Mapsで検索できる固有名詞のみ
- 以下は除外（重複禁止）: {excl}

【カテゴリ多様性（必須）】
なるべく以下のカテゴリから混ぜて提案:
  自然（公園・海岸・山）/ カフェ・スイーツ / 食事・グルメ / 体験・観光 / ショッピング

【目的】{purpose or "観光・おでかけ"}

JSON配列のみで返答（説明不要）:
[
  {{"name":"施設名","types":["cafe"],"address":"{area}","rating":4.2}},
  {{"name":"施設名","types":["restaurant"],"address":"{area}","rating":4.0}}
]

typesの例: cafe, restaurant, park, museum, tourist_attraction, shopping_mall, art_gallery, bakery, spa"""

    try:
        resp = _call_gemini_timed(client, {"model": model, "contents": prompt})
        text = resp.text if resp else ""

        excl_lower = {e.lower() for e in exclude_names}
        result: list[dict[str, Any]] = []

        # Attempt structured parse first
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").removeprefix("json").strip()
        s = cleaned.find("[")
        e_idx = cleaned.rfind("]")
        if s != -1 and e_idx != -1:
            try:
                data = json.loads(cleaned[s : e_idx + 1])
                for item in data[:n]:
                    if isinstance(item, str):
                        name, types_list, address, rating = item.strip(), [], area, 4.0
                    elif isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        types_list = item.get("types") or []
                        address = str(item.get("address") or area)
                        try:
                            rating = float(item.get("rating") or 4.0)
                        except (TypeError, ValueError):
                            rating = 4.0
                    else:
                        continue
                    if not name or name.lower() in excl_lower:
                        continue
                    result.append({
                        "name":         name,
                        "address":      address,
                        "rating":       rating,
                        "review_count": 0,
                        "photo_url":    "",
                        "types":        types_list,
                        "price":        "不明",
                        "place_id":     "",
                        "source":       "gemini",
                    })
            except Exception:
                pass

        # Fallback: name-only list
        if not result:
            names = parse_spot_names(text)
            for name in names[:n]:
                name = name.strip()
                if not name or name.lower() in excl_lower:
                    continue
                result.append({
                    "name":         name,
                    "address":      area,
                    "rating":       4.0,
                    "review_count": 0,
                    "photo_url":    "",
                    "types":        [],
                    "price":        "不明",
                    "place_id":     "",
                    "source":       "gemini",
                })

        logger.info("[GeminiSpots] %d件の補完スポット取得", len(result))
        return result[:n]
    except Exception as exc:
        logger.warning("[GeminiSpots] 補完スポット取得失敗: %s", exc)
        return []


def generate_recommendations(search: dict[str, str], weather: dict[str, Any]) -> list[dict[str, Any]]:
    t_total = time.perf_counter()
    logger.info("=== generate_recommendations 開始 search=%s ===", search)

    from utils import api_cache
    from services.plan_builder import build_all_plans

    try:
        # ── キャッシュ確認（日付込みキー → 毎日違うプラン） ────────────────
        gen_cache_key = api_cache.make_key(
            "gen",
            search.get("place", ""), search.get("people", ""),
            search.get("purpose", ""), search.get("start_time", ""),
            search.get("end_time", ""), search.get("budget", ""),
            datetime.date.today().isoformat(),
        )
        cached = api_cache.cache_get(gen_cache_key, ttl_hours=6)
        if cached is not None:
            logger.info("[Cache] 生成結果HIT → %d件返す", len(cached))
            return cached

        # ── Step 1: Places API ──────────────────────────────────────────────
        places_key = (os.getenv("PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        places: list[dict] = []
        t1 = time.perf_counter()
        if places_key:
            try:
                from services.places_service import fetch_spots
                places = fetch_spots(
                    place          = search.get("place", ""),
                    people         = search.get("people", ""),
                    purpose        = search.get("purpose", ""),
                    weather_indoor = "屋内" in (weather.get("recommendation") or ""),
                )
                logger.info("[Step1] Places: %d件 (%.1fs)", len(places), time.perf_counter() - t1)
            except Exception:
                logger.exception("[Step1] Places API 例外")
        else:
            logger.warning("[Step1] Places APIキー未設定")

        if not places:
            logger.warning("[Step1] Places データなし → フォールバック")
            return fallback_recommendations(search, weather)

        # ── Step 1.5: Gemini補完 (Places が15件未満の場合) ─────────────────
        if len(places) < 15:
            needed = max(5, 15 - len(places))
            logger.info("[Step1.5] Places %d件 < 15件 → Gemini補完 %d件要求", len(places), needed)
            t15 = time.perf_counter()
            extra = suggest_fill_spots(
                area          = search.get("place", ""),
                people        = search.get("people", ""),
                purpose       = search.get("purpose", ""),
                n             = needed,
                exclude_names = [p["name"] for p in places],
            )
            places.extend(extra)
            logger.info("[Step1.5] Gemini補完完了 +%d件 → 合計%d件 (%.1fs)",
                        len(extra), len(places), time.perf_counter() - t15)

        # ── Step 1.8: SNS・トレンド情報取得（非同期・失敗無視） ──────────────
        location = search.get("place", "")
        trend_data: dict = {}
        if location:
            t_trend = time.perf_counter()
            try:
                from services.trend_service import get_trend_data
                trend_data = get_trend_data(location)
                logger.info("[Step1.8] トレンド取得: %d件 (%.1fs)",
                            len(trend_data), time.perf_counter() - t_trend)
            except Exception as e:
                logger.warning("[Step1.8] トレンド取得失敗（無視）: %s", e)
        # search に注入して build_all_plans に引き渡す
        search = dict(search)
        search["trend_data"] = trend_data

        # ── Step 2: Python plan builder — 10テーマ1日コース生成 ────────────
        t2 = time.perf_counter()
        normalized = build_all_plans(places, search, weather)
        logger.info("[Step2] PlanBuilder: %d件 (%.2fs)", len(normalized), time.perf_counter() - t2)

        if not normalized:
            logger.warning("[Step2] プラン生成0件 → フォールバック")
            return fallback_recommendations(search, weather)

        api_cache.cache_set(gen_cache_key, normalized)
        logger.info("=== 最終結果 %d プラン (%s〜%s) ===",
                    len(normalized), search.get("start_time"), search.get("end_time"))
        return normalized

    finally:
        logger.info("[Timing] 総時間: %.1f秒", time.perf_counter() - t_total)


def _inject_places_data(plans: list[dict], places: list[dict]) -> None:
    """Overwrite photo_url / rating / address / map-link with real Places API values."""
    by_name: dict[str, dict] = {}
    for p in places:
        n = p["name"]
        by_name[n] = p
        by_name[n.lower()] = p

    for plan in plans:
        pname = plan.get("name", "")
        gp = (
            by_name.get(pname)
            or by_name.get(pname.lower())
            or next((p for p in places if pname in p["name"] or p["name"] in pname), None)
        )
        if not gp:
            continue
        if gp.get("photo_url"):
            plan["photo_url"] = gp["photo_url"]
        if gp.get("rating") and float(gp["rating"]) > 0:
            plan["rating"] = float(gp["rating"])
        if gp.get("address"):
            plan["address"] = gp["address"]
        pid = gp.get("place_id", "")
        if pid and isinstance(plan.get("links"), dict):
            plan["links"]["map"]        = f"https://www.google.com/maps/place/?q=place_id:{pid}"
            plan["links"]["directions"] = f"https://www.google.com/maps/dir/?api=1&destination=place_id:{pid}"


def _plans_from_places(
    places: list[dict],
    search: dict[str, str],
    weather: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Gemini が完全に失敗した場合でも Places データから直接 1日コースを生成する。
    スポットを2グループに分け、2つの day-plan を返す。
    """
    area = search.get("place", "")
    candidates = [p for p in places if area[:2] in (p.get("address") or "")]
    if len(candidates) < 5:
        candidates = places
    top = candidates[:14]

    mid = max(5, len(top) // 2)
    groups = [
        ("エリアおすすめコース", [s["name"] for s in top[:mid]]),
        ("穴場スポットコース",   [s["name"] for s in top[mid:mid + 7]]),
    ]

    result = []
    for title, names in groups:
        if not names:
            continue
        plan = _build_day_plan({"plan_title": title, "spots": names}, places, search, weather)
        if plan["spots"]:
            result.append(plan)

    if not result:
        all_names = [s["name"] for s in top[:7]]
        plan = _build_day_plan({"plan_title": "おすすめコース", "spots": all_names}, places, search, weather)
        if plan["spots"]:
            result.append(plan)

    logger.info("[PlacesFallback] Places から %d コース生成", len(result))
    return result


def _normalize_simple_format(
    items: list[dict],
    search: dict[str, str],
    weather: dict[str, Any],
    places: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert Gemini's simple [{start, end, title, description}] output into
    the full plan structure expected by templates. Each item becomes one plan.
    Places data is looked up by title for photo, rating, address, and map links.
    """
    by_name: dict[str, dict] = {}
    for p in places:
        by_name[p["name"]] = p
        by_name[p["name"].lower()] = p

    area   = search.get("place", "")
    result: list[dict[str, Any]] = []

    for item in items:
        title = str(item.get("title") or "").strip()
        start = str(item.get("start") or "").strip()
        end   = str(item.get("end")   or "").strip()
        desc  = str(item.get("description") or "").strip()

        if not title or not start:
            continue

        gp = (
            by_name.get(title)
            or by_name.get(title.lower())
            or next((p for p in places if title in p["name"] or p["name"] in title), None)
        )

        photo_url = gp.get("photo_url", "") if gp else ""
        rating    = float(gp.get("rating") or 4.0) if gp else 4.0
        address   = gp.get("address", area) if gp else area
        pid       = gp.get("place_id", "") if gp else ""
        price     = gp.get("price", "不明") if gp else "不明"

        links = build_links(title, title, title, "", address)
        if pid:
            links["map"]        = f"https://www.google.com/maps/place/?q=place_id:{pid}"
            links["directions"] = f"https://www.google.com/maps/dir/?api=1&destination=place_id:{pid}"

        if not photo_url:
            photo_url = photo_url_for_spot([], "スポット", "", name=title, place=area)

        result.append({
            "id":               plan_id(title, area),
            "name":             title,
            "category":         "indoor",
            "weather_tags":     ["sunny", "cloud"],
            "rain_ok":          False,
            "type":             "スポット",
            "area":             area,
            "address":          address,
            "access":           "",
            "hours":            "要確認",
            "budget":           search.get("budget", "要確認"),
            "price":            price,
            "rating":           rating,
            "stay_time":        "1〜2時間",
            "sns_reason":       "",
            "trending_reason":  "",
            "audience":         "",
            "sns_appeal":       "",
            "recommended_time": "",
            "weather_match":    "",
            "highlight":        desc,
            "schedule_items":   [{
                "time":       start,
                "end_time":   end,
                "place":      title,
                "activity":   desc or "訪問・観光",
                "travel_min": 15,
            }],
            "description":      desc,
            "tags":             ["おでかけ", "今日行ける"],
            "links":            links,
            "photo_url":        photo_url,
        })

    logger.info("[SimpleFormat] %d件のプランを変換", len(result))
    return result


def _make_filler_plan(spot: dict[str, Any], search: dict[str, str]) -> dict[str, Any]:
    """Create a minimal unscheduled plan from a Places spot (for schedule gap filling)."""
    area  = search.get("place", "")
    name  = spot["name"]
    addr  = spot.get("address", area)
    pid   = spot.get("place_id", "")
    links = build_links(name, name, name, "", addr)
    if pid:
        links["map"]        = f"https://www.google.com/maps/place/?q=place_id:{pid}"
        links["directions"] = f"https://www.google.com/maps/dir/?api=1&destination=place_id:{pid}"
    return {
        "id":               plan_id(name, area),
        "name":             name,
        "category":         "indoor",
        "weather_tags":     ["sunny", "cloud"],
        "rain_ok":          False,
        "type":             "スポット",
        "area":             area,
        "address":          addr,
        "access":           "",
        "hours":            "要確認",
        "budget":           search.get("budget", "要確認"),
        "price":            spot.get("price", "不明"),
        "rating":           float(spot.get("rating") or 4.0),
        "stay_time":        "1〜2時間",
        "sns_reason":       "",
        "trending_reason":  "",
        "audience":         "",
        "sns_appeal":       "",
        "recommended_time": "",
        "weather_match":    "",
        "highlight":        f"Googleレビュー★{spot.get('rating', 0):.1f}（{spot.get('review_count', 0)}件）",
        "schedule_items":   [],   # unscheduled — filled by _ensure_full_day_schedule
        "description":      addr,
        "tags":             ["おでかけ", "今日行ける"],
        "links":            links,
        "photo_url":        spot.get("photo_url", ""),
    }


def _filter_by_prefecture(plans: list[dict], place: str) -> list[dict]:
    """Remove plans whose address/area clearly belong to a different prefecture."""
    # List of all prefectures to detect wrong ones
    all_prefs = [
        "北海道","青森県","岩手県","宮城県","秋田県","山形県","福島県",
        "茨城県","栃木県","群馬県","埼玉県","千葉県","東京都","神奈川県",
        "新潟県","富山県","石川県","福井県","山梨県","長野県",
        "岐阜県","静岡県","愛知県","三重県",
        "滋賀県","京都府","大阪府","兵庫県","奈良県","和歌山県",
        "鳥取県","島根県","岡山県","広島県","山口県",
        "徳島県","香川県","愛媛県","高知県",
        "福岡県","佐賀県","長崎県","熊本県","大分県","宮崎県","鹿児島県","沖縄県",
    ]
    other_prefs = [p for p in all_prefs if p != place]

    def is_wrong_pref(plan: dict) -> bool:
        address = plan.get("address", "") or ""
        area    = plan.get("area", "") or ""
        text    = address + " " + area
        # If another prefecture name appears in the address → wrong
        for op in other_prefs:
            if op in text:
                return True
        return False

    filtered = [p for p in plans if not is_wrong_pref(p)]
    return filtered if filtered else plans  # if all filtered out, return originals


def _is_abstract(name: str) -> bool:
    # Placeholder characters → definitely abstract
    if "〇〇" in name or "○○" in name or "△△" in name:
        return True
    # Generic descriptive prefixes embedded in the name
    for w in ("周辺の", "近くの", "地元の", "おすすめの", "エリアの", "付近の", "体験型", "アウトドア体験"):
        if w in name:
            return True
    # Bare single-word generic nouns with no proper name attached
    if name.strip() in {"施設", "スポット", "展望台", "公園", "ビーチ", "レストラン",
                        "カフェ", "海岸", "ショッピングモール"}:
        return True
    return False


# ── Prompt ────────────────────────────────────────────────────────────────────

def _prompt_to_min(hhmm: str) -> int:
    try:
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m
    except Exception:
        return 0


def build_prompt(search: dict[str, str], weather: dict[str, Any]) -> str:
    rec = weather.get("recommendation", "")
    if "屋内" in rec:
        weather_priority = "屋内で楽しめる施設（カフェ・体験ワークショップ・美術館・ギャラリー・サウナ・屋内アクティビティ等）を優先的に7件"
    else:
        weather_priority = "屋外で楽しめるスポット（絶景・海岸・公園・街歩き・展望台・アウトドアアクティビティ等）を優先的に7件"

    place      = search.get("place", "東京都")
    start_time = search.get("start_time", "10:00")
    end_time   = search.get("end_time",   "17:00")
    duration   = calc_duration(start_time, end_time)
    dur_str    = f"{int(duration)}時間" if duration == int(duration) else f"{duration}時間"
    min_slots  = max(3, round(duration / 1.5))

    # Build duration-appropriate type guidance
    if duration <= 2:
        type_guide = (
            f"利用可能時間は{dur_str}です。近場で完結できるスポットのみ提案してください。\n"
            "  適切: カフェ、神社仏閣、展望台、美術館・博物館（滞在1〜2時間以内）\n"
            "  禁止: テーマパーク、動物園、水族館（半日〜終日かかる施設）"
        )
    elif duration <= 4:
        type_guide = (
            f"利用可能時間は{dur_str}です。半日で楽しめるスポットを提案してください。\n"
            "  適切: 美術館、博物館、水族館（小〜中規模）、市場・商店街、城・歴史地区\n"
            "  禁止: テーマパーク（ハウステンボス・USJ等、最低5〜8時間必要な施設）"
        )
    elif duration <= 6:
        type_guide = (
            f"利用可能時間は{dur_str}です。ほぼ一日かけて楽しめるスポットを提案してください。\n"
            "  適切: 動物園、水族館（大規模）、テーマパーク（中規模）、自然公園、温泉リゾート"
        )
    else:
        type_guide = (
            f"利用可能時間は{dur_str}です。終日楽しめる大型スポットも含めて提案してください。\n"
            "  適切: テーマパーク（ハウステンボス・USJ等）、大型動物園・水族館、島・自然観光地"
        )

    pref_name = place  # e.g. "大分県"
    people    = search.get("people", "")
    purpose   = search.get("purpose", "")

    _m = datetime.date.today().month
    if _m in [3, 4, 5]:
        season_str = f"春（{_m}月・桜/新緑シーズン）"
    elif _m in [6, 7, 8]:
        season_str = f"夏（{_m}月・海/祭り/花火シーズン）"
    elif _m in [9, 10, 11]:
        season_str = f"秋（{_m}月・紅葉/収穫シーズン）"
    else:
        season_str = f"冬（{_m}月・イルミネーション/温泉シーズン）"

    _SEASONAL_GUIDE: dict[int, tuple[str, str]] = {
        1:  ("初詣スポット、温泉、冬グルメ、雪景色",
             "桜名所、紅葉スポット、海水浴場、ひまわり畑、あじさい"),
        2:  ("梅の名所、バレンタイン限定カフェ、温泉、冬景色",
             "桜名所、紅葉スポット、イチョウ並木、あじさい、海水浴"),
        3:  ("梅・早咲き桜、春の花畑、新緑スポット",
             "紅葉スポット、イチョウ並木（外苑等）、スキー場、イルミネーション"),
        4:  ("桜の名所、花見スポット、チューリップ畑、春祭り",
             "紅葉スポット、イチョウ並木（外苑等）、スキー場"),
        5:  ("新緑、藤の名所、バラ園、GW向けスポット",
             "紅葉スポット、イチョウ並木、スキー場"),
        6:  ("紫陽花・あじさい名所、花菖蒲、ホタル観賞、梅雨の屋内施設",
             "紅葉スポット、イチョウ並木（外苑等）、桜名所、スキー場"),
        7:  ("ひまわり畑、海水浴・海辺スポット、花火大会周辺、涼しい屋内施設",
             "紅葉スポット、イチョウ並木（外苑等）、桜名所、スキー場"),
        8:  ("海水浴・海辺スポット、花火、ひまわり、涼しい高原・屋内施設",
             "紅葉スポット、イチョウ並木（外苑等）、桜名所、スキー場"),
        9:  ("コスモス、秋の花畑、早めの紅葉（高山）、秋グルメ",
             "桜名所、ひまわり、あじさい、海水浴、スキー場"),
        10: ("紅葉名所、コスモス、秋祭り、ハロウィンイベント",
             "桜名所、ひまわり畑、あじさい、スキー場"),
        11: ("紅葉・イチョウ並木（神宮外苑等）、秋の景色、温泉",
             "桜名所、ひまわり、あじさい、海水浴、夏祭り"),
        12: ("イルミネーション、クリスマスイベント、温泉、冬グルメ",
             "桜名所、ひまわり、あじさい、海水浴"),
    }
    _season_rec, _season_avoid = _SEASONAL_GUIDE.get(_m, ("", ""))
    variety_seed = random.randint(1, 999)

    if "カップル" in people or "デート" in purpose:
        people_guide = (
            "【カップル向け必須】夜景スポット・映えカフェ・隠れ家レストラン・ドライブスポット"
            "・フォトジェニックな場所・期間限定イベント・花畑・体験工房・温泉\n"
            "  ❌ 禁止パターン: 子供向け遊具施設・動物園・大型ショッピングモールが中心"
        )
    elif "家族" in people:
        people_guide = (
            "【家族向け必須】体験型施設・動物ふれあい・大型公園・室内遊び場・科学館"
            "・ファームパーク・プラネタリウム・工場見学・アスレチック\n"
            "  ❌ 禁止パターン: 深夜営業・バー・若者向けSNSカフェのみ"
        )
    elif "友達" in people:
        people_guide = (
            "【友達グループ向け必須】食べ歩きエリア・サウナ・アウトドアアクティビティ"
            "（カヤック/クライミング/SUP等）・絶景フォトスポット・話題グルメ・脱出ゲーム・バーベキュー\n"
            "  ❌ 禁止パターン: 静かなカップル向け施設・一人向け施設のみ"
        )
    else:  # ひとり
        people_guide = (
            "【ひとり向け必須】こだわりカフェ・温泉・美術館・ギャラリー・自然ハイキング"
            "・聖地巡礼・グルメ一人飯・読書スポット・創作体験・サウナ\n"
            "  ❌ 禁止パターン: 大人数前提のアトラクション・テーマパークのみ"
        )

    start_min    = _prompt_to_min(start_time)
    end_min_val  = _prompt_to_min(end_time)
    required_min = start_min + round((end_min_val - start_min) * 0.9)
    required_end = f"{required_min // 60:02d}:{required_min % 60:02d}"

    return f"""
あなたは以下のGoogle/Instagram/TikTok検索クエリに精通したSNSトレンドリサーチャーです。
「観光ガイドブックの定番スポット」ではなく「SNS検索で今実際に上位に出てくる場所」を提案してください。

━━━━━━━━━━━━━━━━━━━━━━━━
【絶対厳守：都道府県制限】
━━━━━━━━━━━━━━━━━━━━━━━━
提案はすべて「{pref_name}」内の施設のみ。address は「{pref_name}」から始めること。
足りない場合は {pref_name} 内の映えカフェ・話題グルメ・絶景スポット・体験施設で補完。
━━━━━━━━━━━━━━━━━━━━━━━━

【Step 1: SNS検索シミュレーション（候補プールの作成）】

以下の検索クエリで上位に出てくるスポットを候補として集めてください:

  🔍「{pref_name} 話題スポット 2025 2026」
  🔍「{pref_name} TikTok人気スポット」
  🔍「{pref_name} Instagram 映えスポット」
  🔍「{pref_name} 人気カフェ おすすめ」
  🔍「{pref_name} 映えスポット」
  🔍「{pref_name} 最近オープン 話題」
  🔍「{pref_name} 若者 人気 {season_str}」
  🔍「{pref_name} {people} おすすめ」

これらの検索で上位に出てくるような場所を候補プールとして集める。
定番の大型動物園・水族館・ショッピングモールは上記検索で上位に出る場合のみ許可（最大2件）。

━━━━━━━━━━━━━━━━━━━━━━━━

【Step 2: 条件フィルタリング】

候補プールから以下の条件で10件を選ぶ:

◆ 同行者・目的
{people_guide}

◆ 利用時間
{type_guide}
・スケジュール開始: {start_time} / 終了上限: {end_time}

◆ その他
- 同行者: {people} / 気分: {purpose}
- 予算: {search.get("budget", "気にしない")}
- 天気: {weather.get("condition")} {weather.get("temperature")}℃ / 降水{weather.get("rain_chance")}%
- {rec}

━━━━━━━━━━━━━━━━━━━━━━━━

【季節適合ルール（最優先・厳守）】
━━━━━━━━━━━━━━━━━━━━━━━━
現在: {_m}月 / {season_str}

✅ 今すすめるべきスポット（優先）:
  {_season_rec}

❌ 今の季節に合わない・絶対禁止:
  {_season_avoid}

重要: 季節外れのスポット（例: 6月に「外苑いちょう並木」「紅葉名所」）は絶対に提案しないこと。
「今行く価値がある」と感じられるスポットを選ぶこと。
━━━━━━━━━━━━━━━━━━━━━━━━

【レコメンド優先順位（厳守）】

  1位 今の季節・{_m}月に旬のスポット（季節限定イベント・花・景色）
  2位 SNS（Instagram/TikTok）で話題のスポット
  3位 TikTok人気スポット（バズ動画・バイラル）
  4位 Instagram人気スポット（映え・フォト需要）
  5位 Googleレビュー高評価の穴場スポット（4.2以上）
  6位 定番観光地（最大2件まで）

バリエーション: {variety_seed}（毎回異なる組み合わせを提案）

10件の内訳（厳守）:
  ✅ SNS話題・映えスポット（カフェ/フォトスポット/夜景）: 3〜4件
  ✅ グルメ・食べ歩き・飲み処・話題レストラン: 2〜3件
  ✅ 体験型・アクティビティ・ワークショップ: 1〜2件
  ✅ 自然・絶景・季節スポット: 1〜2件
  ❌ 動物園・水族館・大型ショッピングモール: 合計2件まで（3件以上は禁止）

━━━━━━━━━━━━━━━━━━━━━━━━

【フィールドルール】
- name: Google Mapsで検索できる実在する固有名詞（「近くのカフェ」等は禁止）
- schedule_items の place も実在施設名のみ
- sns_reason: 「〇〇動画が〇万回再生」「〇〇新エリアがオープン」など具体的に（1文）
- trending_reason: 2026年現在なぜ注目されているか（1〜2文）
- audience: 特に人気な層（例: 20代カップル・インスタ女子・地元若者）
- sns_appeal: 具体的な映えポイント・構図・おすすめ時間帯（1文）
- address は必ず「{pref_name}」で始める / rating は 4.0〜4.9

━━━━━━━━━━━━━━━━━━━━━━━━

【schedule_itemsルール（厳守）】
━━ 絶対ルール: {start_time}〜{end_time}を隙間なく埋めること ━━
【必達目標】利用時間の90%以上を埋めること（最低 {required_end} まで予定を組むこと）
- 終了条件は current_time >= {end_time} のみ。それ以外で終了禁止
- スポット追加ルール:
    ① current_time = {start_time} でスタート
    ② スポット追加 → end_time = current_time + 滞在時間（最大120分）
    ③ end_time > {end_time} になりそうなら → end_time = {end_time} として最後のアイテムに
    ④ next_time = end_time + travel_min（5分単位丸め）→ ② に戻る
    ⑤ current_time >= {end_time} になったら終了
- スロット数: 最低{min_slots}件。{end_time}まで埋め切るために必要な数だけ追加すること
- 1スポットあたり最大120分（2時間）を超えない ← 厳守
- 滞在時間の目安（これを超えない）:
    展望台・絶景 → 60〜90分 / カフェ・スイーツ → 45〜60分
    食事（ランチ・ディナー）→ 45〜75分 / 街歩き・フォトスポット → 30〜60分
    神社仏閣・史跡 → 30〜60分 / ショッピング → 60〜90分
    体験施設・アクティビティ → 90〜120分 / バー・居酒屋 → 60〜90分
- 食事・カフェ休憩を必ず1〜2回挟む
- 【禁止】{end_time}前に終わること / 早期終了 / アイテム数不足 / 満足度や件数での終了

━━━━━━━━━━━━━━━━━━━━━━━━

出力はJSON配列のみ（10件）:
[
  {{
    "name": "施設の正式名称",
    "category": "indoor または outdoor",
    "weather_tags": ["sunny"/"rain"/"cloud" から選んで配列],
    "rain_ok": true または false,
    "type": "カフェ / グルメ / 映えスポット / 展望台 / 体験施設 / 神社仏閣 / テーマパーク など",
    "area": "最寄り駅や地区名",
    "address": "{pref_name}から始まる住所",
    "access": "最寄り駅から徒歩○分 または 車で○分",
    "hours": "営業時間",
    "budget": "一人あたりの目安",
    "rating": 4.3,
    "stay_time": "滞在目安（例: 1〜2時間）",
    "sns_reason": "Instagram/TikTokで話題の具体的な理由（1文）",
    "trending_reason": "2026年現在なぜ注目されているか（最近の出来事・バズ理由を1〜2文）",
    "audience": "特に人気な層（例: 20代カップル・インスタ女子・地元若者）",
    "sns_appeal": "具体的な映えポイント・構図・おすすめ時間帯（1文）",
    "recommended_time": "おすすめ訪問時間帯",
    "weather_match": "今日の天気との相性（1文）",
    "highlight": "最大の魅力（1文）",
    "schedule_items": [
      {{"time": "{start_time}", "end_time": "HH:MM（60〜90分後）",   "place": "施設A",       "activity": "見学・体験（具体的に）", "travel_min": 15}},
      {{"time": "HH:MM",        "end_time": "HH:MM（45〜60分後）",   "place": "カフェB",     "activity": "カフェ・ランチ",         "travel_min": 10}},
      {{"time": "HH:MM",        "end_time": "HH:MM（60〜90分後）",   "place": "施設C",       "activity": "観光・体験",             "travel_min": 15}},
      {{"time": "HH:MM",        "end_time": "HH:MM（45〜75分後）",   "place": "グルメD",     "activity": "夕食・飲み",             "travel_min": 10}},
      {{"time": "HH:MM",        "end_time": "{end_time}",            "place": "施設E",       "activity": "最後の活動",             "travel_min": 0}}
    ],
    "description": "スポットの説明（2文程度）",
    "map_query": "施設名そのまま",
    "instagram_tag": "ハッシュタグ用文字列（スペースなし）",
    "official_url": "公式サイトURL（不明なら空文字）",
    "photo_keyword": "英語のキーワード",
    "tags": ["タグ1", "タグ2", "タグ3"]
  }}
]
""".strip()


def build_prompt_with_places(
    search: dict[str, str],
    weather: dict[str, Any],
    places: list[dict[str, Any]],
) -> str:
    """
    Gemini に「テーマ別コース × 3」のスポット名リストだけを返させる。
    時間割は Python 側が完全制御するため、プロンプトに時間は含めない。
    Output: [{"plan_title":"テーマ","spots":["名前1","名前2",...]}]
    """
    pref_name = search.get("place", "東京都")
    people    = search.get("people", "")
    purpose   = search.get("purpose", "")

    spot_list = "\n".join(f"{i+1}. {p['name']}" for i, p in enumerate(places[:20]))

    return f"""次の候補スポットを使って、{pref_name}での1日コースを3種類作ってください。

【候補スポット】
{spot_list}

【ルール】
- 各コースに5〜7スポットを選ぶ（訪問順に並べる）
- リスト外のスポット名は使用禁止
- コース間でテーマを明確に変える
- 同行者:{people} / 気分:{purpose}

【出力（JSONのみ、説明不要）】
[
  {{"plan_title":"テーマ名1","spots":["スポット名1","スポット名2","スポット名3","スポット名4","スポット名5"]}},
  {{"plan_title":"テーマ名2","spots":["スポット名A","スポット名B","スポット名C","スポット名D","スポット名E"]}},
  {{"plan_title":"テーマ名3","spots":["スポット名X","スポット名Y","スポット名Z","スポット名W","スポット名V"]}}
]""".strip()


# ── Parsing & normalizing ─────────────────────────────────────────────────────

def parse_plans(text: str | None) -> list[dict[str, Any]]:
    if not text:
        raise ValueError("Gemini response is empty")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("JSON array not found")

    return json.loads(cleaned[start : end + 1])


def parse_spot_names(text: str | None) -> list[str]:
    """Geminiが返した ["スポット名1", "スポット名2", ...] を解析する。
    文字列リストのほか、{"title":...} / {"name":...} の辞書配列も許容する。
    """
    if not text:
        return []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    s = cleaned.find("[")
    e = cleaned.rfind("]")
    if s == -1 or e == -1:
        return []
    try:
        data = json.loads(cleaned[s : e + 1])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, str):
            n = item.strip()
        elif isinstance(item, dict):
            n = str(item.get("title") or item.get("name") or "").strip()
        else:
            continue
        if n:
            names.append(n)
    return names


def parse_courses(text: str | None) -> list[dict]:
    """Geminiが返した [{plan_title, spots:[name1, name2, ...]}] を解析する。"""
    if not text:
        return []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    s = cleaned.find("[")
    e = cleaned.rfind("]")
    if s == -1 or e == -1:
        return []
    try:
        data = json.loads(cleaned[s : e + 1])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title  = str(item.get("plan_title") or item.get("title") or "").strip()
        spots  = item.get("spots") or []
        names  = [str(n).strip() for n in spots if isinstance(n, str) and n.strip()]
        if title and names:
            result.append({"plan_title": title, "spots": names})
    return result


def normalize_plans(
    plans: list[dict[str, Any]],
    search: dict[str, str],
    weather: dict[str, Any],
    places: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    # Detect simple format: [{"start":..., "end":..., "title":..., "description":...}]
    if plans and isinstance(plans[0], dict) and "start" in plans[0] and "title" in plans[0]:
        result = _normalize_simple_format(plans, search, weather, places or [])
        return result if result else fallback_recommendations(search, weather)

    normalized = []
    for plan in plans[:10]:
        name        = str(plan.get("name")         or "おすすめスポット")
        area        = str(plan.get("area")         or search.get("place") or "周辺")
        plan_type   = str(plan.get("type")         or "スポット")
        category    = str(plan.get("category")     or "indoor")
        map_query   = str(plan.get("map_query")    or name)
        ig_tag      = str(plan.get("instagram_tag") or name)
        official    = str(plan.get("official_url") or "")
        photo_kw    = str(plan.get("photo_keyword") or "")
        address     = str(plan.get("address")      or area)
        tags        = normalize_tags(plan.get("tags"))
        wtags       = normalize_weather_tags(plan.get("weather_tags"))

        try:
            rating = float(plan.get("rating") or 4.0)
            rating = max(1.0, min(5.0, rating))
        except (TypeError, ValueError):
            rating = 4.0

        rain_ok = bool(plan.get("rain_ok", category == "indoor"))
        schedule_items = normalize_schedule_items(plan.get("schedule_items"))

        normalized.append({
            "id":               plan_id(name, area),
            "name":             name,
            "category":         category,
            "weather_tags":     wtags,
            "rain_ok":          rain_ok,
            "type":             plan_type,
            "area":             area,
            "address":          address,
            "access":           str(plan.get("access")           or f"{area}周辺"),
            "hours":            str(plan.get("hours")            or "要確認"),
            "budget":           str(plan.get("budget")           or search.get("budget") or "要確認"),
            "rating":           rating,
            "stay_time":        str(plan.get("stay_time")        or "1〜2時間"),
            "sns_reason":       str(plan.get("sns_reason")       or ""),
            "trending_reason":  str(plan.get("trending_reason")  or plan.get("sns_reason") or ""),
            "audience":         str(plan.get("audience")         or ""),
            "sns_appeal":       str(plan.get("sns_appeal")       or ""),
            "recommended_time": str(plan.get("recommended_time") or ""),
            "weather_match":    str(plan.get("weather_match")    or weather.get("summary") or ""),
            "highlight":        str(plan.get("highlight")        or ""),
            "schedule_items":   schedule_items,
            "description":      str(plan.get("description")      or ""),
            "tags":             tags,
            "links":            build_links(name, map_query, ig_tag, official, address),
            "photo_url":        photo_url_for_spot(tags, plan_type, photo_kw, name=name, place=area),
        })

    if not normalized:
        return fallback_recommendations(search, weather)
    return normalized


def normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, list):
        values = [str(t) for t in tags if t]
    else:
        values = []
    return (values + ["おでかけ", "今日行ける"])[:3]


def normalize_weather_tags(tags: Any) -> list[str]:
    valid = {"sunny", "rain", "cloud"}
    if isinstance(tags, list):
        result = [t for t in tags if t in valid]
        return result if result else ["sunny", "cloud"]
    return ["sunny", "cloud"]


def normalize_schedule_items(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            travel = int(item.get("travel_min") or 0)
        except (TypeError, ValueError):
            travel = 0
        result.append({
            "time":       str(item.get("time")     or ""),
            "end_time":   str(item.get("end_time") or ""),
            "place":      str(item.get("place")    or ""),
            "activity":   str(item.get("activity") or ""),
            "travel_min": travel,
        })
    # Back-fill end_times from next item's start when missing
    for i, s in enumerate(result):
        if not s["end_time"] and i + 1 < len(result) and result[i + 1]["time"]:
            nxt_h, nxt_m = map(int, result[i + 1]["time"].split(":"))
            mv = result[i]["travel_min"]
            et_min = nxt_h * 60 + nxt_m - mv
            if et_min > 0:
                s["end_time"] = f"{et_min // 60:02d}:{et_min % 60:02d}"
    return result  # no hard cap — let the full schedule through


# ── 1日コース ビルダー ──────────────────────────────────────────────────────────

def _build_day_plan(
    course: dict,
    places: list[dict[str, Any]],
    search: dict[str, str],
    weather: dict[str, Any],
) -> dict[str, Any]:
    """
    {plan_title, spots:[name, ...]} を受け取り、Python 側でタイムスロットを割り当てて
    テンプレートが要求するフル plan dict を返す。

    スロット計算:
      slot_min = clamp((total_min - (n-1)*15) / n, 60, 120)
    全スポット消化後に残り時間があれば未使用 Places スポットで補完する。
    """
    plan_title = course.get("plan_title", "おすすめコース")
    spot_names = course.get("spots", [])
    start_time = search.get("start_time", "10:00")
    end_time   = search.get("end_time",   "17:00")
    area       = search.get("place", "")

    def _to_min(hhmm: str) -> int:
        try:
            h, m = map(int, hhmm.split(":"))
            return h * 60 + m
        except Exception:
            return 0

    def _from_min(mins: int) -> str:
        mins = max(0, min(mins, 23 * 60 + 59))
        return f"{mins // 60:02d}:{mins % 60:02d}"

    by_name: dict[str, dict] = {}
    for p in places:
        by_name[p["name"]] = p
        by_name[p["name"].lower()] = p

    def _lookup(name: str) -> dict | None:
        return (
            by_name.get(name)
            or by_name.get(name.lower())
            or next((p for p in places if name in p["name"] or p["name"] in name), None)
        )

    start_min  = _to_min(start_time)
    end_min    = _to_min(end_time)
    total_min  = max(end_min - start_min, 60)
    n          = max(len(spot_names), 1)
    slot_min   = max(60, min(120, (total_min - (n - 1) * 15) // n))

    built_spots: list[dict] = []
    used_lower: set[str]    = set()
    current = start_min

    def _append_spot(name: str, t_start: int, t_end: int, travel: int) -> None:
        gp        = _lookup(name)
        photo_url = gp.get("photo_url", "") if gp else ""
        rating    = float(gp.get("rating") or 4.0) if gp else 4.0
        address   = gp.get("address", area) if gp else area
        pid       = gp.get("place_id", "") if gp else ""
        price     = gp.get("price", "不明") if gp else "不明"
        lnks      = build_links(name, name, name, "", address)
        if pid:
            lnks["map"]        = f"https://www.google.com/maps/place/?q=place_id:{pid}"
            lnks["directions"] = f"https://www.google.com/maps/dir/?api=1&destination=place_id:{pid}"
        if not photo_url:
            photo_url = photo_url_for_spot([], "スポット", "", name=name, place=area)
        built_spots.append({
            "name":       name,
            "start_time": _from_min(t_start),
            "end_time":   _from_min(t_end),
            "travel_min": travel,
            "price":      price,
            "rating":     rating,
            "photo_url":  photo_url,
            "address":    address,
            "links":      lnks,
        })
        used_lower.add(name.lower())

    # フェーズ1: Gemini 選定スポットを割り当て
    for name in spot_names:
        if current >= end_min:
            break
        t_end  = min(current + slot_min, end_min)
        travel = 15 if t_end < end_min else 0
        _append_spot(name, current, t_end, travel)
        current = t_end + travel

    # フェーズ2: 残り時間を未使用 Places スポットで補完
    if current < end_min - 30:
        for spot in places:
            if current >= end_min:
                break
            if spot["name"].lower() in used_lower:
                continue
            name   = spot["name"]
            t_end  = min(current + slot_min, end_min)
            travel = 15 if t_end < end_min else 0
            _append_spot(name, current, t_end, travel)
            current = t_end + travel

    # フェーズ3: それでも短ければ最終スポットを end_time まで延長
    if built_spots and current < end_min:
        built_spots[-1]["end_time"]   = _from_min(end_min)
        built_spots[-1]["travel_min"] = 0

    # ── backward compat フィールド（saved.html / today.html 向け） ────────
    schedule_items = [
        {
            "time":       s["start_time"],
            "end_time":   s["end_time"],
            "place":      s["name"],
            "activity":   "訪問・観光",
            "travel_min": s["travel_min"],
        }
        for s in built_spots
    ]
    first_photo = next((s["photo_url"] for s in built_spots if s.get("photo_url")), "")
    avg_rating  = round(sum(s["rating"] for s in built_spots) / max(len(built_spots), 1), 1)
    first_links = built_spots[0]["links"] if built_spots else build_links(plan_title, plan_title, plan_title, "", area)
    duration_h  = int((end_min - start_min) / 60)

    logger.info("[DayPlan] '%s' %d件スポット (%s〜%s)", plan_title, len(built_spots), start_time, end_time)

    return {
        "id":               plan_id(plan_title, area),
        "plan_title":       plan_title,
        "name":             plan_title,
        "spots":            built_spots,
        "schedule_items":   schedule_items,
        "start_time":       start_time,
        "end_time":         end_time,
        "total_spots":      len(built_spots),
        "photo_url":        first_photo,
        "rating":           avg_rating,
        "type":             "1日コース",
        "category":         "mixed",
        "weather_tags":     ["sunny", "cloud"],
        "rain_ok":          False,
        "area":             area,
        "address":          area,
        "access":           "",
        "hours":            f"{start_time}〜{end_time}",
        "budget":           search.get("budget", "要確認"),
        "price":            "スポットにより異なる",
        "stay_time":        f"{duration_h}時間",
        "highlight":        plan_title,
        "description":      f"{len(built_spots)}スポットの1日コース ({start_time}〜{end_time})",
        "trending_reason":  "",
        "audience":         "",
        "sns_appeal":       "",
        "sns_reason":       "",
        "recommended_time": "",
        "weather_match":    "",
        "tags":             ["1日コース", "おでかけ", "今日行ける"],
        "links":            first_links,
    }


# ── Python-controlled slot scheduler ─────────────────────────────────────────

def _assign_slots_to_spots(
    spot_names: list[str],
    places: list[dict[str, Any]],
    search: dict[str, str],
    weather: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Gemini が選んだスポット名リストに対して、Python 側でタイムスロットを均等割り当てする。
    start_time〜end_time を必ず埋める（AI に時間を委ねない）。

    手順:
      1. slot_min = total_min / n_spots (60〜120 に丸め)
      2. start_time から順に各スポットへスロットを割り当て
      3. Gemini のスポットが尽きたら未使用 Places スポットで補完
      4. それでも短ければ最終スポットを end_time まで延長
    """
    start_time = search.get("start_time", "10:00")
    end_time   = search.get("end_time",   "17:00")
    area       = search.get("place", "")

    def _to_min(hhmm: str) -> int:
        try:
            h, m = map(int, hhmm.split(":"))
            return h * 60 + m
        except Exception:
            return 0

    def _from_min(mins: int) -> str:
        mins = max(0, min(mins, 23 * 60 + 59))
        return f"{mins // 60:02d}:{mins % 60:02d}"

    start_min = _to_min(start_time)
    end_min   = _to_min(end_time)
    total_min = max(end_min - start_min, 60)

    # Places 名前→データ の高速ルックアップ
    by_name: dict[str, dict] = {}
    for p in places:
        by_name[p["name"]] = p
        by_name[p["name"].lower()] = p

    def _lookup(name: str) -> dict | None:
        return (
            by_name.get(name)
            or by_name.get(name.lower())
            or next((p for p in places if name in p["name"] or p["name"] in name), None)
        )

    def _build_plan(name: str, t_start: int, t_end: int, travel: int) -> dict[str, Any]:
        gp        = _lookup(name)
        photo_url = gp.get("photo_url", "") if gp else ""
        rating    = float(gp.get("rating") or 4.0) if gp else 4.0
        address   = gp.get("address", area) if gp else area
        pid       = gp.get("place_id", "") if gp else ""
        price     = gp.get("price", "不明") if gp else "不明"
        links     = build_links(name, name, name, "", address)
        if pid:
            links["map"]        = f"https://www.google.com/maps/place/?q=place_id:{pid}"
            links["directions"] = f"https://www.google.com/maps/dir/?api=1&destination=place_id:{pid}"
        if not photo_url:
            photo_url = photo_url_for_spot([], "スポット", "", name=name, place=area)
        stay_min = t_end - t_start
        return {
            "id":               plan_id(name, area),
            "name":             name,
            "category":         "indoor",
            "weather_tags":     ["sunny", "cloud"],
            "rain_ok":          False,
            "type":             "スポット",
            "area":             area,
            "address":          address,
            "access":           "",
            "hours":            "要確認",
            "budget":           search.get("budget", "要確認"),
            "price":            price,
            "rating":           rating,
            "stay_time":        f"約{stay_min}分",
            "sns_reason":       "",
            "trending_reason":  "",
            "audience":         "",
            "sns_appeal":       "",
            "recommended_time": "",
            "weather_match":    "",
            "highlight":        f"Googleレビュー★{rating:.1f}",
            "schedule_items":   [{
                "time":       _from_min(t_start),
                "end_time":   _from_min(t_end),
                "place":      name,
                "activity":   "訪問・観光",
                "travel_min": travel,
            }],
            "description":      address,
            "tags":             ["おでかけ", "今日行ける"],
            "links":            links,
            "photo_url":        photo_url,
        }

    # スロットサイズを計算 (60〜120 分)
    n_spots  = max(len(spot_names), 1)
    slot_min = max(60, min(120, total_min // n_spots))

    result: list[dict[str, Any]] = []
    used_names: set[str] = set()
    current = start_min

    # ── フェーズ1: Gemini 選定スポットを割り当て ───────────────────────────
    for name in spot_names:
        if current >= end_min:
            break
        t_end  = min(current + slot_min, end_min)
        travel = 15 if t_end < end_min else 0
        result.append(_build_plan(name, current, t_end, travel))
        used_names.add(name.lower())
        current = t_end + travel
        logger.info("[Slots] %s: %s〜%s", name, _from_min(t_end - (t_end - current + travel)), _from_min(t_end))

    # ── フェーズ2: 残り時間を未使用 Places スポットで補完 ─────────────────
    if current < end_min - 30:
        for spot in places:
            if current >= end_min:
                break
            if spot["name"].lower() in used_names:
                continue
            name   = spot["name"]
            t_end  = min(current + slot_min, end_min)
            travel = 15 if t_end < end_min else 0
            result.append(_build_plan(name, current, t_end, travel))
            used_names.add(name.lower())
            current = t_end + travel
            logger.info("[Slots補完] %s: 〜%s", name, _from_min(t_end))

    # ── フェーズ3: それでも短ければ最終スポットを end_time まで延長 ────────
    if result and current < end_min - 30:
        last_item = result[-1]["schedule_items"][-1]
        old_end   = last_item["end_time"]
        last_item["end_time"]   = _from_min(end_min)
        last_item["travel_min"] = 0
        result[-1]["stay_time"] = f"約{end_min - _to_min(last_item['time'])}分"
        logger.info("[Slots補完] 最終スポット end_time延長: %s → %s", old_end, _from_min(end_min))

    logger.info("[Slots] 割当完了 %d件 (%s〜%s)", len(result), start_time, end_time)
    return result


# ── Full-day schedule guarantor ───────────────────────────────────────────────

def _ensure_full_day_schedule(
    plans: list[dict],
    start_time: str,
    end_time: str,
) -> list[dict]:
    """
    Guarantee plans collectively cover start_time → end_time.

    - Plans that already have schedule_items are kept as-is (sorted by start)
    - Plans without schedule_items receive sequential time slots
    - Termination: ONLY current_time >= end_time
    - Spots that would overshoot end_time by >30 min are SKIPPED (not terminators)
    """
    def _to_min(hhmm: str) -> int:
        try:
            if not hhmm:
                return 0
            h, m = map(int, hhmm.split(":"))
            return h * 60 + m
        except Exception:
            return 0

    def _from_min(mins: int) -> str:
        mins = max(0, min(mins, 23 * 60 + 59))
        return f"{mins // 60:02d}:{mins % 60:02d}"

    def _first_min(p: dict) -> int:
        items = p.get("schedule_items") or []
        if items and items[0].get("time"):
            return _to_min(items[0]["time"])
        return 99 * 60  # unscheduled → sort last

    end_min = _to_min(end_time)

    # Sort: already-scheduled plans first (by their start time), unscheduled last
    plans = sorted(plans, key=_first_min)

    # Find where the scheduled plans end
    current = _to_min(start_time)
    for p in plans:
        for item in (p.get("schedule_items") or []):
            item_end = _to_min(item.get("end_time") or "")
            mv       = int(item.get("travel_min") or 0)
            if item_end + mv > current:
                current = item_end + mv

    # Fill remaining time with plans that have no schedule yet
    for p in plans:
        if current >= end_min:  # ← ONLY termination condition
            break
        if p.get("schedule_items"):
            continue  # already scheduled

        # Default stay duration by spot type
        t = (p.get("type") or "").lower()
        if any(k in t for k in ("カフェ", "café", "cafe", "coffee", "スイーツ")):
            stay = 50
        elif any(k in t for k in ("食事", "ランチ", "ディナー", "グルメ", "レストラン", "restaurant")):
            stay = 60
        elif any(k in t for k in ("体験", "アクティビティ", "ワークショップ", "テーマパーク")):
            stay = 90
        elif any(k in t for k in ("展望台", "絶景", "夜景", "observatory")):
            stay = 75
        elif any(k in t for k in ("神社", "仏閣", "寺", "shrine", "temple")):
            stay = 45
        else:
            stay = 70

        # Skip spots that would overshoot end_time by more than 30 min
        if current + stay > end_min + 30:
            continue  # skip — keep looping for shorter-stay spots

        end_this = min(current + stay, end_min)
        travel   = 15 if end_this < end_min else 0
        p["schedule_items"] = [{
            "time":       _from_min(current),
            "end_time":   _from_min(end_this),
            "place":      p["name"],
            "activity":   "訪問・観光",
            "travel_min": travel,
        }]
        logger.info("[Schedule補完] %s: %s〜%s", p["name"], _from_min(current), _from_min(end_this))
        current = end_this + travel

    # ── 全プランスケジュール済みでも end_time に届いていないケースを修正 ────────
    # スポットが尽きても残り時間がある場合、最終スポットの end_time を最大 3h まで延長する
    if current < end_min - 30:
        sorted_plans = sorted(
            [p for p in plans if p.get("schedule_items")],
            key=_first_min,
        )
        if sorted_plans:
            last_plan  = sorted_plans[-1]
            last_items = last_plan["schedule_items"]
            last_item  = last_items[-1]
            old_end    = last_item.get("end_time", "")
            # 延長上限: 現在の end_time から 3h まで
            cap_min    = _to_min(old_end) + 180
            new_end    = min(end_min, cap_min)
            last_item["end_time"]   = _from_min(new_end)
            last_item["travel_min"] = 0
            logger.info(
                "[Schedule補完] 最終スポット '%s' end_time延長: %s → %s",
                last_item.get("place", last_plan["name"]), old_end, _from_min(new_end),
            )

    return sorted(plans, key=_first_min)


# ── Real spots DB (fallback when no API key) ──────────────────────────────────

_SPOTS_DB: dict[str, list[dict]] = {
    "東京都": [
        {"name": "東京スカイツリー", "type": "展望台", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "押上・スカイツリー前", "access": "とうきょうスカイツリー駅から徒歩1分",
         "hours": "10:00〜22:00", "rating": 4.3, "stay_time": "1〜2時間",
         "highlight": "高さ634mの日本一の電波塔から東京を一望できる絶景スポット",
         "description": "東京の新名所。天望デッキからは関東平野を360度見渡せる。",
         "sns_reason": "ガラス張りの床越しに見下ろす絶景がInstagramで話題",
         "recommended_time": "夕方〜夜のライトアップ時間帯がおすすめ",
         "tags": ["展望台", "観光", "夜景"], "photo_keyword": "tokyo skytree night",
         "official_url": "https://www.tokyo-skytree.jp/",
         "schedule_items": [
             {"time": "17:00", "place": "東京スカイツリー 天望デッキ", "activity": "夕暮れの東京を一望"},
             {"time": "18:30", "place": "東京ソラマチ", "activity": "ショッピング・夕食"},
             {"time": "19:30", "place": "東京スカイツリー 天望回廊", "activity": "ライトアップされた夜景を撮影"},
         ]},
        {"name": "浅草寺・仲見世通り", "type": "神社仏閣", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "浅草", "access": "浅草駅から徒歩5分",
         "hours": "6:00〜17:00（境内は24時間）", "rating": 4.5, "stay_time": "1〜2時間",
         "highlight": "東京最古の寺と日本最長の仲見世商店街が織りなす下町情緒",
         "description": "628年創建の都内最古の寺。雷門から続く仲見世通りには老舗が並ぶ。",
         "sns_reason": "雷門と仲見世の和の雰囲気がInstagramで人気のフォトスポット",
         "recommended_time": "早朝〜午前中が空いていておすすめ",
         "tags": ["観光", "歴史", "グルメ"], "photo_keyword": "asakusa temple japan",
         "official_url": "https://www.senso-ji.jp/",
         "schedule_items": [
             {"time": "10:00", "place": "浅草寺 雷門", "activity": "記念撮影・仲見世散策"},
             {"time": "11:00", "place": "浅草寺 本堂", "activity": "参拝とおみくじ"},
             {"time": "12:00", "place": "駒形どぜう", "activity": "老舗でどじょう鍋ランチ"},
             {"time": "13:30", "place": "花やしき", "activity": "日本最古の遊園地を見学"},
         ]},
        {"name": "teamLab Planets TOKYO", "type": "体験施設", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "豊洲", "access": "新豊洲駅から徒歩2分",
         "hours": "10:00〜22:00（最終入場21:00）", "rating": 4.6, "stay_time": "1〜1.5時間",
         "highlight": "水に入るデジタルアートが圧倒的なSNS映えを生み出す体験型美術館",
         "description": "豊洲にある没入型デジタルアート空間。水に入りながら光の世界を体験できる。",
         "sns_reason": "水面に映るデジタルアートがTikTok・Instagramで何万回も再生される話題スポット",
         "recommended_time": "平日午前中が比較的空いている",
         "tags": ["アート", "体験", "SNS映え"], "photo_keyword": "teamlab digital art tokyo",
         "official_url": "https://planets.teamlab.art/tokyo/",
         "schedule_items": [
             {"time": "10:00", "place": "teamLab Planets TOKYO", "activity": "水のアート空間を体験・撮影"},
             {"time": "12:00", "place": "豊洲千客万来", "activity": "海鮮丼ランチ"},
             {"time": "13:30", "place": "豊洲市場 場外", "activity": "新鮮なシーフードのはしご"},
         ]},
        {"name": "渋谷スクランブルスクエア展望施設 SHIBUYA SKY", "type": "展望台", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "渋谷", "access": "渋谷駅直結",
         "hours": "10:00〜23:00", "rating": 4.5, "stay_time": "1〜1.5時間",
         "highlight": "渋谷スクランブル交差点を真上から見下ろせる都内屈指の展望スポット",
         "description": "230mの高さから渋谷の交差点・富士山・東京タワーまで一望できる。",
         "sns_reason": "スクランブル交差点を真上から撮れる唯一の展望台としてSNSで話題",
         "recommended_time": "夕暮れ〜夜の時間帯が最も美しい",
         "tags": ["展望台", "渋谷", "夜景"], "photo_keyword": "shibuya sky observation tokyo",
         "official_url": "https://www.shibuya-scramble-square.com/sky/",
         "schedule_items": [
             {"time": "18:00", "place": "SHIBUYA SKY", "activity": "夕暮れの渋谷と富士山を一望"},
             {"time": "19:30", "place": "渋谷ストリーム", "activity": "渋谷川沿いでディナー"},
             {"time": "21:00", "place": "渋谷スクランブル交差点", "activity": "賑わう夜の交差点を散歩"},
         ]},
        {"name": "上野動物園", "type": "動物園", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "上野", "access": "上野駅から徒歩5分",
         "hours": "9:30〜17:00（月曜休園）", "rating": 4.2, "stay_time": "3〜4時間",
         "highlight": "パンダが見られる日本最古の動物園。約3,000頭の動物が生息",
         "description": "1882年開園の日本初の動物園。ジャイアントパンダが人気の的。",
         "sns_reason": "パンダの寝姿・食事シーンがSNSで毎日バズる定番スポット",
         "recommended_time": "開園直後の午前中がパンダを近くで見るチャンス",
         "tags": ["動物園", "家族", "パンダ"], "photo_keyword": "ueno zoo panda japan",
         "official_url": "https://www.tokyo-zoo.net/zoo/ueno/",
         "schedule_items": [
             {"time": "9:30", "place": "上野動物園 正門", "activity": "開園に合わせて入園"},
             {"time": "10:00", "place": "パンダ舎", "activity": "ジャイアントパンダを鑑賞・撮影"},
             {"time": "12:00", "place": "上野精養軒", "activity": "老舗レストランでランチ"},
             {"time": "13:30", "place": "上野動物園 西園", "activity": "ゴリラ・ゾウを見学"},
         ]},
        {"name": "お台場・ダイバーシティ東京", "type": "ショッピング", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "お台場", "access": "台場駅から徒歩1分",
         "hours": "11:00〜21:00", "rating": 4.1, "stay_time": "2〜3時間",
         "highlight": "実物大ユニコーンガンダム立像と東京湾の夜景が楽しめる複合施設",
         "description": "日本最大級のガンダムが立つ話題のショッピングモール。フードコートも充実。",
         "sns_reason": "ガンダム立像との記念写真がInstagramでバズる人気スポット",
         "recommended_time": "夜のライトアップ時間（18:00〜21:00）がおすすめ",
         "tags": ["ショッピング", "お台場", "ガンダム"], "photo_keyword": "odaiba tokyo shopping",
         "official_url": "https://mitsui-shopping-park.com/divercity-tokyo/",
         "schedule_items": [
             {"time": "14:00", "place": "ダイバーシティ東京", "activity": "ショッピング・グルメを満喫"},
             {"time": "17:00", "place": "ユニコーンガンダム立像", "activity": "実物大ガンダムを撮影"},
             {"time": "18:30", "place": "デックス東京ビーチ", "activity": "レインボーブリッジを望む夕食"},
         ]},
    ],
    "神奈川県": [
        {"name": "横浜赤レンガ倉庫", "type": "観光・ショッピング", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "横浜みなとみらい", "access": "馬車道駅から徒歩6分",
         "hours": "11:00〜20:00（施設による）", "rating": 4.3, "stay_time": "1〜2時間",
         "highlight": "明治時代の赤レンガとみなとみらいの夜景が重なる横浜随一の絶景スポット",
         "description": "1911年竣工の歴史的建造物。ショップやレストランが入り横浜港を一望できる。",
         "sns_reason": "夕日と赤レンガのコントラストがInstagramで何度もバズる定番フォトスポット",
         "recommended_time": "夕方〜夜のライトアップ時間帯",
         "tags": ["観光", "夜景", "デート"], "photo_keyword": "yokohama red brick warehouse",
         "official_url": "https://www.yokohama-akarenga.jp/",
         "schedule_items": [
             {"time": "16:00", "place": "横浜赤レンガ倉庫", "activity": "レトロな倉庫を散策・お土産購入"},
             {"time": "17:30", "place": "横浜ハンマーヘッド", "activity": "クラフトビールと夕食"},
             {"time": "19:00", "place": "山下公園", "activity": "ライトアップされた港の夜景を散歩"},
         ]},
        {"name": "新江ノ島水族館", "type": "水族館", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "江の島・片瀬海岸", "access": "片瀬江ノ島駅から徒歩3分",
         "hours": "10:00〜18:00", "rating": 4.4, "stay_time": "2〜3時間",
         "highlight": "クラゲファンタジーホールの幻想的な展示がSNSで人気の水族館",
         "description": "相模湾を再現した大水槽とクラゲの展示が圧巻。湘南の海が目前に広がる立地。",
         "sns_reason": "ネオンカラーのクラゲ水槽がInstagramで数万いいねを獲得する映えスポット",
         "recommended_time": "平日の午前中が空いていてゆっくり見られる",
         "tags": ["水族館", "クラゲ", "SNS映え"], "photo_keyword": "enoshima aquarium jellyfish",
         "official_url": "https://www.enosui.com/",
         "schedule_items": [
             {"time": "10:00", "place": "新江ノ島水族館", "activity": "相模湾大水槽とクラゲ展示を鑑賞"},
             {"time": "12:30", "place": "片瀬西浜海水浴場", "activity": "海辺でランチ・散歩"},
             {"time": "14:00", "place": "江の島", "activity": "江島神社参拝・展望台からの眺め"},
         ]},
        {"name": "鎌倉大仏殿高徳院", "type": "神社仏閣", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "鎌倉・長谷", "access": "長谷駅から徒歩10分",
         "hours": "8:00〜17:30", "rating": 4.5, "stay_time": "1〜1.5時間",
         "highlight": "高さ13.35mの国宝大仏。鎌倉を代表する1252年建立の歴史遺産",
         "description": "鎌倉の象徴、阿弥陀如来像。大仏の胎内に入る体験もできる。",
         "sns_reason": "背景の空と大仏の写真がInstagramで常に人気のテッパン観光スポット",
         "recommended_time": "午前中の早い時間帯が空いていて光も良い",
         "tags": ["歴史", "観光", "鎌倉"], "photo_keyword": "kamakura great buddha",
         "official_url": "https://www.kotoku-in.jp/",
         "schedule_items": [
             {"time": "9:00", "place": "鎌倉大仏殿高徳院", "activity": "国宝大仏を参拝・胎内拝観"},
             {"time": "10:30", "place": "長谷寺", "activity": "あじさいの名所を散策"},
             {"time": "12:00", "place": "小町通り", "activity": "鎌倉グルメの食べ歩き"},
             {"time": "14:00", "place": "鶴岡八幡宮", "activity": "鎌倉最大の神社を参拝"},
         ]},
        {"name": "横浜中華街", "type": "グルメ", "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "横浜・元町中華街", "access": "元町・中華街駅から徒歩1分",
         "hours": "店舗により異なる（11:00〜22:00頃）", "rating": 4.3, "stay_time": "2〜3時間",
         "highlight": "日本最大の中華街。500店以上が軒を連ねる食の聖地",
         "description": "横浜中華街は日本最大の中国人街。本格中華から食べ歩きグルメまで揃う。",
         "sns_reason": "豪華な飲茶や食べ歩きグルメがInstagram・TikTokで毎日バズる",
         "recommended_time": "ランチタイム（11:30〜13:30）または夕食（17:00〜19:00）がおすすめ",
         "tags": ["グルメ", "中華", "食べ歩き"], "photo_keyword": "yokohama chinatown japan",
         "official_url": "https://www.chinatown.or.jp/",
         "schedule_items": [
             {"time": "11:30", "place": "聘珍楼", "activity": "本格飲茶ランチ"},
             {"time": "13:00", "place": "横浜中華街 食べ歩き", "activity": "小籠包・マンゴープリンを食べ歩き"},
             {"time": "14:30", "place": "元町ショッピングストリート", "activity": "おしゃれなショップでウィンドウショッピング"},
         ]},
    ],
    "大阪府": [
        {"name": "ユニバーサル・スタジオ・ジャパン", "type": "テーマパーク", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "此花区・ユニバーサルシティ", "access": "ユニバーサルシティ駅から徒歩3分",
         "hours": "9:00〜21:00（時期による）", "rating": 4.6, "stay_time": "6〜8時間",
         "highlight": "ハリウッド映画の世界に飛び込む日本最大級のテーマパーク",
         "description": "ハリーポッター、マリオなど人気エリアが充実。スリリングなアトラクション多数。",
         "sns_reason": "マリオエリアのリアルなゲーム世界がInstagram・TikTokで常に話題",
         "recommended_time": "平日開園直後が最も空いていておすすめ",
         "tags": ["テーマパーク", "USJ", "アトラクション"], "photo_keyword": "universal studios japan osaka",
         "official_url": "https://www.usj.co.jp/",
         "schedule_items": [
             {"time": "9:00", "place": "USJ スーパー・ニンテンドー・ワールド", "activity": "マリオエリアのアトラクションを満喫"},
             {"time": "12:00", "place": "ウィザーディング・ワールド・オブ・ハリー・ポッター", "activity": "バタービールとハリポタ体験"},
             {"time": "15:00", "place": "ハリウッド・ドリーム・ザ・ライド", "activity": "絶叫アトラクションを体験"},
             {"time": "19:00", "place": "USJパレード鑑賞スポット", "activity": "夜のパレードを鑑賞"},
         ]},
        {"name": "道頓堀", "type": "グルメ・観光", "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "難波・道頓堀", "access": "難波駅から徒歩3分",
         "hours": "終日（店舗は11:00〜24:00頃）", "rating": 4.4, "stay_time": "2〜3時間",
         "highlight": "グリコのネオン看板と大阪グルメが集結する大阪最大の繁華街",
         "description": "たこ焼き・お好み焼き・串カツなど大阪B級グルメの聖地。",
         "sns_reason": "グリコ看板前での記念写真がSNSで大阪観光の定番として毎日投稿される",
         "recommended_time": "夜のネオンが輝く20:00以降がおすすめ",
         "tags": ["グルメ", "大阪", "食べ歩き"], "photo_keyword": "dotonbori osaka glico sign",
         "official_url": "",
         "schedule_items": [
             {"time": "18:00", "place": "道頓堀 たこ焼きコナモン博物館", "activity": "元祖たこ焼きを食べ比べ"},
             {"time": "19:30", "place": "ずぼらや フグ料理", "activity": "大阪名物ふぐ料理でディナー"},
             {"time": "21:00", "place": "道頓堀グリコサイン", "activity": "ネオン輝く大阪夜景を撮影"},
         ]},
        {"name": "海遊館", "type": "水族館", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "天保山・港区", "access": "大阪港駅から徒歩8分",
         "hours": "10:00〜20:00", "rating": 4.5, "stay_time": "2〜3時間",
         "highlight": "世界最大級の水槽でジンベエザメと泳ぐ魚たちを間近で観察できる",
         "description": "太平洋の旅をコンセプトにした世界有数の大型水族館。ジンベエザメが目玉。",
         "sns_reason": "ジンベエザメと一緒に撮った写真がInstagramで常に人気のフォトスポット",
         "recommended_time": "平日の午前中が空いていてゆっくり見られる",
         "tags": ["水族館", "ジンベエザメ", "家族"], "photo_keyword": "osaka aquarium kaiyukan",
         "official_url": "https://www.kaiyukan.com/",
         "schedule_items": [
             {"time": "10:00", "place": "海遊館", "activity": "太平洋大水槽でジンベエザメを鑑賞"},
             {"time": "12:30", "place": "天保山マーケットプレース", "activity": "海の幸ランチ"},
             {"time": "14:00", "place": "天保山大観覧車", "activity": "大阪湾と街を一望"},
         ]},
        {"name": "あべのハルカス展望台 ハルカス300", "type": "展望台", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "阿倍野・天王寺", "access": "天王寺駅直結",
         "hours": "9:00〜22:00", "rating": 4.4, "stay_time": "1〜1.5時間",
         "highlight": "日本一高いビル（300m）の最上階から大阪・神戸・奈良まで見渡せる",
         "description": "高さ300mの超高層ビル。晴天時は六甲山から明石海峡まで眺望できる。",
         "sns_reason": "雲海が生まれる朝の絶景がSNSで話題、天空の都市写真が話題",
         "recommended_time": "晴れた日の夕方〜夜がおすすめ",
         "tags": ["展望台", "大阪", "夜景"], "photo_keyword": "harukas 300 osaka observation",
         "official_url": "https://www.abenoharukas-300.jp/observatory/",
         "schedule_items": [
             {"time": "17:30", "place": "ハルカス300 展望台", "activity": "大阪の夕景と夜景を一望"},
             {"time": "19:00", "place": "天王寺公園・てんしば", "activity": "芝生広場でくつろぐ"},
             {"time": "20:00", "place": "新世界 通天閣", "activity": "大阪の下町文化を体験"},
         ]},
    ],
    "京都府": [
        {"name": "伏見稲荷大社", "type": "神社仏閣", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "伏見区・稲荷", "access": "稲荷駅から徒歩すぐ",
         "hours": "24時間（自由参拝）", "rating": 4.6, "stay_time": "2〜3時間",
         "highlight": "約1万基の朱色の鳥居が連なる、日本で最もInstagramで撮影されている絶景",
         "description": "全国約3万社ある稲荷神社の総本社。千本鳥居のトンネルは圧巻の美しさ。",
         "sns_reason": "千本鳥居が日本のSNS・外国人SNSで最も多くシェアされる観光スポット",
         "recommended_time": "早朝（6:00〜8:00）が人が少なく幻想的な写真が撮れる",
         "tags": ["神社", "鳥居", "SNS映え"], "photo_keyword": "fushimi inari torii gates kyoto",
         "official_url": "https://inari.jp/",
         "schedule_items": [
             {"time": "7:00", "place": "伏見稲荷大社", "activity": "早朝の千本鳥居を撮影"},
             {"time": "9:00", "place": "伏見稲荷大社 奥社", "activity": "山頂まで登拝"},
             {"time": "11:00", "place": "錦市場", "activity": "京の台所で食べ歩き"},
             {"time": "13:00", "place": "嵐山 天龍寺", "activity": "世界遺産の庭園を散策"},
         ]},
        {"name": "嵐山・竹林の小径", "type": "自然・観光", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "嵯峨野・嵐山", "access": "嵐山駅から徒歩5分",
         "hours": "終日（自由散策）", "rating": 4.5, "stay_time": "2〜3時間",
         "highlight": "天に向かって伸びる竹の緑と木漏れ日が絶景の京都必訪スポット",
         "description": "嵯峨野の竹林は京都観光の定番。天龍寺の世界遺産庭園も隣接。",
         "sns_reason": "竹のトンネルが作り出す幻想的な光がInstagramで常にバイラル",
         "recommended_time": "早朝か夕方の光が差し込む時間帯",
         "tags": ["自然", "京都", "SNS映え"], "photo_keyword": "arashiyama bamboo grove kyoto",
         "official_url": "",
         "schedule_items": [
             {"time": "8:00", "place": "嵐山 竹林の小径", "activity": "朝の竹林を散策・撮影"},
             {"time": "9:30", "place": "天龍寺", "activity": "世界遺産庭園を観覧"},
             {"time": "11:00", "place": "渡月橋", "activity": "桂川と嵐山の絶景を楽しむ"},
             {"time": "12:30", "place": "嵯峨豆腐 三忠", "activity": "湯豆腐の老舗でランチ"},
         ]},
        {"name": "清水寺", "type": "神社仏閣", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "東山区", "access": "清水道バス停から徒歩10分",
         "hours": "6:00〜18:00（季節により変動）", "rating": 4.7, "stay_time": "1.5〜2時間",
         "highlight": "「清水の舞台から飛び降りる」の語源。崖に張り出す木造の舞台が絶景",
         "description": "創建798年の世界遺産。清水の舞台から京都市街を一望できる。",
         "sns_reason": "桜・紅葉の季節の清水舞台写真がInstagramで毎シーズン話題に",
         "recommended_time": "開門直後の朝6:00〜7:00が最も幻想的",
         "tags": ["世界遺産", "京都", "歴史"], "photo_keyword": "kiyomizu temple kyoto",
         "official_url": "https://www.kiyomizudera.or.jp/",
         "schedule_items": [
             {"time": "6:30", "place": "清水寺", "activity": "朝の清水舞台を参拝・撮影"},
             {"time": "8:00", "place": "産寧坂・二寧坂", "activity": "石畳の古道を散策・食べ歩き"},
             {"time": "10:00", "place": "八坂神社", "activity": "祇園の守護神に参拝"},
             {"time": "11:30", "place": "京都祇園 にし家", "activity": "にしんそばで昼食"},
         ]},
    ],
    "北海道": [
        {"name": "旭山動物園", "type": "動物園", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "旭川市", "access": "旭川駅からバスで40分",
         "hours": "10:30〜17:15（夏季）9:30〜16:30（冬季）", "rating": 4.5, "stay_time": "3〜4時間",
         "highlight": "ペンギンの空飛ぶ散歩・アザラシの垂直泳ぎが見られる日本一の動物園",
         "description": "行動展示で知られる日本有数の動物園。冬のペンギン散歩は世界的に有名。",
         "sns_reason": "ペンギンの散歩とアザラシの円筒水槽がSNSで毎冬バイラルになる絶景",
         "recommended_time": "冬季（12月〜3月）のペンギン散歩タイム（11:00と14:30）",
         "tags": ["動物園", "ペンギン", "旭川"], "photo_keyword": "asahiyama zoo penguin hokkaido",
         "official_url": "https://www.city.asahikawa.hokkaido.jp/asahiyamazoo/",
         "schedule_items": [
             {"time": "10:30", "place": "旭山動物園 入口", "activity": "ペンギン館でペンギンを観察"},
             {"time": "11:00", "place": "旭山動物園 ペンギン散歩コース", "activity": "ペンギンの散歩を間近で見学"},
             {"time": "13:00", "place": "旭川ラーメン村", "activity": "旭川醤油ラーメンでランチ"},
             {"time": "14:30", "place": "旭山動物園 あざらし館", "activity": "アザラシの縦泳ぎを鑑賞"},
         ]},
        {"name": "函館山展望台", "type": "展望台", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "函館市", "access": "函館山ロープウェイ山麓駅から3分",
         "hours": "10:00〜22:00（ロープウェイ運行時間）", "rating": 4.7, "stay_time": "1〜1.5時間",
         "highlight": "世界三大夜景のひとつ。函館の夜景は「100万ドルの夜景」と称される",
         "description": "函館山山頂から見る函館の夜景は世界的に有名。ロープウェイで3分で到達。",
         "sns_reason": "砂時計型に輝く函館の夜景がInstagramで世界中から投稿される憧れの絶景",
         "recommended_time": "日没直後（夏：20:00頃、冬：17:00頃）が最も美しい",
         "tags": ["夜景", "函館", "世界三大夜景"], "photo_keyword": "hakodate night view japan",
         "official_url": "",
         "schedule_items": [
             {"time": "19:30", "place": "函館山ロープウェイ", "activity": "山頂まで3分で到達"},
             {"time": "20:00", "place": "函館山展望台", "activity": "世界三大夜景を撮影・鑑賞"},
             {"time": "21:00", "place": "函館朝市（夜営業）", "activity": "海鮮料理でディナー"},
         ]},
        {"name": "小樽運河", "type": "観光", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "小樽市", "access": "小樽駅から徒歩10分",
         "hours": "終日（散策自由）", "rating": 4.4, "stay_time": "1.5〜2時間",
         "highlight": "大正時代の石造倉庫とガス灯が並ぶレトロな運河景観が美しい",
         "description": "明治〜大正期の北の商都の面影を残すレトロな運河。冬は雪景色が幻想的。",
         "sns_reason": "ガス灯と石造倉庫のレトロ夜景がInstagramで四季を通じてバズるスポット",
         "recommended_time": "夕方〜夜のガス灯点灯時間帯",
         "tags": ["観光", "レトロ", "運河"], "photo_keyword": "otaru canal hokkaido",
         "official_url": "",
         "schedule_items": [
             {"time": "17:00", "place": "小樽運河", "activity": "レトロな石造倉庫と運河を散策"},
             {"time": "18:30", "place": "小樽堺町通り", "activity": "オルゴール・ガラス工芸品のショッピング"},
             {"time": "19:30", "place": "北の誉酒造", "activity": "小樽の地酒試飲"},
         ]},
    ],
    "沖縄県": [
        {"name": "美ら海水族館", "type": "水族館", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "本部町・海洋博公園", "access": "那覇から車で約2時間",
         "hours": "10:00〜18:00（季節により変動）", "rating": 4.7, "stay_time": "3〜4時間",
         "highlight": "世界最大級の水槽「黒潮の海」でジンベエザメとマンタが優雅に泳ぐ",
         "description": "沖縄県の海洋博公園内にある世界有数の大型水族館。黒潮の海は圧倒的。",
         "sns_reason": "ジンベエザメとマンタが同じ水槽で泳ぐ光景がSNSで沖縄旅行の定番投稿",
         "recommended_time": "開館直後の午前中が空いていてゆっくり見られる",
         "tags": ["水族館", "沖縄", "ジンベエザメ"], "photo_keyword": "okinawa churaumi aquarium",
         "official_url": "https://churaumi.okinawa/",
         "schedule_items": [
             {"time": "10:00", "place": "美ら海水族館 黒潮の海", "activity": "ジンベエザメとマンタを鑑賞"},
             {"time": "12:00", "place": "海洋文化館レストラン", "activity": "海を眺めながらランチ"},
             {"time": "13:30", "place": "エメラルドビーチ", "activity": "美ら海水族館前の白砂ビーチを散歩"},
         ]},
        {"name": "首里城公園", "type": "歴史・観光", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "那覇市首里", "access": "首里駅から徒歩15分",
         "hours": "8:30〜18:00（季節により変動）", "rating": 4.4, "stay_time": "1.5〜2時間",
         "highlight": "琉球王国の威光を伝える朱色の城郭と那覇市街を一望できる世界遺産",
         "description": "15〜19世紀に栄えた琉球王国の政治・文化の中心地。2019年の火災から復元中。",
         "sns_reason": "復元される鮮やかな朱色の正殿がSNSで沖縄歴史観光の代表スポット",
         "recommended_time": "朝の開園直後が観光客が少なくおすすめ",
         "tags": ["世界遺産", "琉球", "歴史"], "photo_keyword": "shuri castle okinawa",
         "official_url": "https://oki-park.jp/shurijo/",
         "schedule_items": [
             {"time": "9:00", "place": "首里城公園 守礼門", "activity": "琉球王国の象徴・守礼門を撮影"},
             {"time": "10:00", "place": "首里城 正殿エリア", "activity": "復元される正殿と歴史展示を見学"},
             {"time": "12:00", "place": "国際通り", "activity": "沖縄料理・ちんすこう食べ歩き"},
         ]},
        {"name": "国際通り", "type": "グルメ・ショッピング", "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "那覇市", "access": "県庁前駅から徒歩すぐ",
         "hours": "店舗により異なる（10:00〜23:00頃）", "rating": 4.2, "stay_time": "2〜3時間",
         "highlight": "沖縄土産・グルメが集まる那覇の目抜き通り「奇跡の1マイル」",
         "description": "約1.6kmの那覇最大のショッピングゾーン。タコライス・紅芋タルトなどが揃う。",
         "sns_reason": "沖縄グルメ食べ歩きとカラフルな土産店がSNSで旅行定番コンテンツ",
         "recommended_time": "夕方〜夜が店が開いており賑やか",
         "tags": ["ショッピング", "グルメ", "沖縄"], "photo_keyword": "kokusai street naha okinawa",
         "official_url": "",
         "schedule_items": [
             {"time": "11:00", "place": "国際通り", "activity": "沖縄土産・泡盛ショッピング"},
             {"time": "12:30", "place": "ステーキハウス88 国際通り店", "activity": "沖縄名物ステーキランチ"},
             {"time": "14:00", "place": "牧志公設市場", "activity": "沖縄の食材・食文化を見学"},
         ]},
    ],
    "長崎県": [
        {"name": "ハウステンボス", "type": "テーマパーク", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "佐世保市", "access": "ハウステンボス駅から徒歩3分",
         "hours": "9:00〜21:00（季節により変動）", "rating": 4.5, "stay_time": "6〜8時間",
         "highlight": "オランダの街並みを再現した日本最大のテーマパーク。花と光のイベントが有名",
         "description": "152万㎡の広大な敷地にオランダの建築・庭園を再現。季節イベントが豊富。",
         "sns_reason": "チューリップ祭りや光の王国のイルミネーションがInstagramで毎シーズン話題",
         "recommended_time": "夕方からの光の王国イルミネーション時間帯",
         "tags": ["テーマパーク", "長崎", "イルミネーション"], "photo_keyword": "huis ten bosch nagasaki",
         "official_url": "https://www.huistenbosch.co.jp/",
         "schedule_items": [
             {"time": "10:00", "place": "ハウステンボス アムステルダムシティ", "activity": "オランダ街並み散策・撮影"},
             {"time": "12:00", "place": "ハウステンボス フードコート", "activity": "長崎ちゃんぽんランチ"},
             {"time": "17:00", "place": "ハウステンボス 光の王国エリア", "activity": "日本最大のイルミネーションを鑑賞"},
         ]},
        {"name": "稲佐山展望台", "type": "展望台", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "長崎市", "access": "長崎駅からバスで20分または稲佐山ロープウェイ",
         "hours": "9:00〜22:00", "rating": 4.5, "stay_time": "1〜1.5時間",
         "highlight": "日本三大夜景に認定された長崎の夜景を一望できる展望スポット",
         "description": "標高333mから眺める長崎の夜景は函館・神戸と並ぶ日本三大夜景のひとつ。",
         "sns_reason": "長崎港を包む夜景が函館と並ぶ日本三大夜景としてSNSで話題",
         "recommended_time": "日没30分後〜1時間後が最も美しい",
         "tags": ["夜景", "長崎", "展望台"], "photo_keyword": "inasayama night view nagasaki",
         "official_url": "",
         "schedule_items": [
             {"time": "18:00", "place": "稲佐山ロープウェイ", "activity": "ロープウェイで山頂へ"},
             {"time": "18:30", "place": "稲佐山展望台", "activity": "長崎三大夜景を撮影・鑑賞"},
             {"time": "20:00", "place": "長崎新地中華街", "activity": "長崎ちゃんぽんと皿うどんディナー"},
         ]},
        {"name": "長崎バイオパーク", "type": "動物園", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "西彼杵郡西海市", "access": "長崎市内から車で約40分",
         "hours": "10:00〜17:00", "rating": 4.6, "stay_time": "3〜4時間",
         "highlight": "カピバラやワオキツネザルと触れ合える日本一フレンドリーな動物公園",
         "description": "動物がフリーで歩き回る自然な形の動物公園。カピバラの露天風呂が有名。",
         "sns_reason": "カピバラが自由に歩き回って触れ合えるSNS動画が毎日バイラルになる",
         "recommended_time": "午前中の給餌タイム前が動物が活発",
         "tags": ["動物", "カピバラ", "触れ合い"], "photo_keyword": "nagasaki biopark capybara",
         "official_url": "https://www.biopark.co.jp/",
         "schedule_items": [
             {"time": "10:00", "place": "長崎バイオパーク", "activity": "カピバラと記念撮影・餌やり"},
             {"time": "12:00", "place": "長崎バイオパーク レストラン", "activity": "自然の中でランチ"},
             {"time": "14:00", "place": "長崎バイオパーク 全エリア", "activity": "ワオキツネザル・フラミンゴと触れ合い"},
         ]},
        {"name": "アミュプラザ長崎", "type": "ショッピング", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "長崎市・長崎駅前", "access": "長崎駅直結",
         "hours": "10:00〜21:00", "rating": 4.0, "stay_time": "2〜3時間",
         "highlight": "長崎駅直結の大型商業施設。屋上から長崎市街と稲佐山を一望できる",
         "description": "映画館・レストラン・ファッションが揃う長崎最大のショッピング施設。",
         "sns_reason": "屋上のデッキから見える稲佐山の夜景がSNSで穴場スポットとして話題",
         "recommended_time": "夕方以降の屋上からの夜景鑑賞がおすすめ",
         "tags": ["ショッピング", "長崎", "雨の日"], "photo_keyword": "nagasaki shopping mall",
         "official_url": "https://www.amu-nagasaki.com/",
         "schedule_items": [
             {"time": "11:00", "place": "アミュプラザ長崎 フードコート", "activity": "長崎グルメランチ"},
             {"time": "13:00", "place": "アミュプラザ長崎 ショッピング", "activity": "長崎土産・ファッション購入"},
             {"time": "18:00", "place": "アミュプラザ長崎 屋上", "activity": "稲佐山と長崎市街の夜景を撮影"},
         ]},
        {"name": "出島", "type": "歴史・観光", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "長崎市", "access": "出島電停から徒歩すぐ",
         "hours": "8:00〜21:00", "rating": 4.2, "stay_time": "1〜1.5時間",
         "highlight": "鎖国時代に唯一の西洋との窓口だった人工島が復元された国際交流の歴史遺産",
         "description": "江戸時代、唯一の西洋交易地として機能した扇形の人工島。建物が復元されている。",
         "sns_reason": "江戸時代の建物が再現された和洋折衷の街並みがSNSでレトロフォトとして人気",
         "recommended_time": "夜のライトアップ（21:00まで）がおすすめ",
         "tags": ["歴史", "長崎", "世界遺産候補"], "photo_keyword": "dejima nagasaki historical",
         "official_url": "https://nagasakidejima.jp/",
         "schedule_items": [
             {"time": "9:00", "place": "出島", "activity": "江戸時代の建物を見学・体験"},
             {"time": "11:00", "place": "長崎新地中華街", "activity": "長崎ちゃんぽんランチ"},
             {"time": "14:00", "place": "グラバー園", "activity": "明治時代の洋館群を見学"},
         ]},
    ],
    "福岡県": [
        {"name": "太宰府天満宮", "type": "神社仏閣", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "太宰府市", "access": "太宰府駅から徒歩5分",
         "hours": "6:00〜19:00（季節により変動）", "rating": 4.5, "stay_time": "1.5〜2時間",
         "highlight": "学問の神・菅原道真公を祀る全国天満宮の総本社。受験シーズンは特に賑わう",
         "description": "1000年の歴史を持つ全国天満宮の総本社。梅の名所としても知られる。",
         "sns_reason": "梅の花と朱色の本殿の写真がInstagramで毎年2月にバイラルになる",
         "recommended_time": "梅の季節（2月〜3月）の早朝がおすすめ",
         "tags": ["神社", "学問", "梅"], "photo_keyword": "dazaifu tenmangu shrine fukuoka",
         "official_url": "https://www.dazaifutenmangu.or.jp/",
         "schedule_items": [
             {"time": "9:00", "place": "太宰府天満宮", "activity": "本殿参拝・おみくじ"},
             {"time": "10:00", "place": "参道", "activity": "梅ケ枝餅の食べ歩き"},
             {"time": "11:00", "place": "九州国立博物館", "activity": "国宝・重文の展示を見学"},
         ]},
        {"name": "キャナルシティ博多", "type": "ショッピング", "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "博多区", "access": "博多駅から徒歩15分またはバスで5分",
         "hours": "10:00〜21:00", "rating": 4.2, "stay_time": "2〜3時間",
         "highlight": "運河が流れるユニークなデザインのショッピングモール。ラーメンスタジアムも有名",
         "description": "独自の都市型商業施設。中央に流れる運河と噴水ショーが話題の人気スポット。",
         "sns_reason": "運河と噴水ショーのダイナミックな映像がSNSで博多観光の定番投稿",
         "recommended_time": "夜の噴水ショー（毎時開催）の時間帯",
         "tags": ["ショッピング", "博多", "雨の日"], "photo_keyword": "canal city hakata fukuoka",
         "official_url": "https://canalcity.co.jp/",
         "schedule_items": [
             {"time": "11:00", "place": "キャナルシティ博多 ラーメンスタジアム", "activity": "九州各地の有名ラーメンを食べ比べ"},
             {"time": "13:00", "place": "キャナルシティ博多 ショッピング", "activity": "ファッション・雑貨ショッピング"},
             {"time": "19:00", "place": "キャナルシティ 中央広場", "activity": "噴水ショーを鑑賞"},
         ]},
    ],
    "愛知県": [
        {"name": "名古屋城", "type": "歴史・観光", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "名古屋市中区", "access": "名城公園駅から徒歩5分",
         "hours": "9:00〜16:30", "rating": 4.4, "stay_time": "1.5〜2時間",
         "highlight": "金のシャチホコが輝く日本三名城のひとつ。本丸御殿の豪華絢爛な障壁画が必見",
         "description": "1612年築城の国宝。復元された本丸御殿には贅を尽くした障壁画が並ぶ。",
         "sns_reason": "金のシャチホコと桜の組み合わせがInstagramで春の名古屋観光写真の定番",
         "recommended_time": "桜の季節（3月下旬〜4月上旬）の朝がおすすめ",
         "tags": ["歴史", "城", "名古屋"], "photo_keyword": "nagoya castle japan",
         "official_url": "https://www.nagoyajo.city.nagoya.jp/",
         "schedule_items": [
             {"time": "9:00", "place": "名古屋城 本丸御殿", "activity": "豪華な障壁画と書院造りを見学"},
             {"time": "11:00", "place": "名古屋城 天守閣", "activity": "名古屋市街を一望"},
             {"time": "12:30", "place": "矢場とん 矢場町本店", "activity": "名古屋名物みそかつランチ"},
         ]},
        {"name": "レゴランド・ジャパン・リゾート", "type": "テーマパーク", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "名古屋市港区", "access": "金城ふ頭駅直結",
         "hours": "10:00〜17:00（季節により変動）", "rating": 4.1, "stay_time": "4〜6時間",
         "highlight": "250万個以上のレゴブロックで作られたアトラクションが揃う子供向けテーマパーク",
         "description": "名古屋港に隣接するレゴブロックのテーマパーク。日本初の本格レゴランド。",
         "sns_reason": "名古屋城のレゴ再現モデルなど精巧な作品がSNSで話題",
         "recommended_time": "平日の午前中が空いていてアトラクションに乗りやすい",
         "tags": ["テーマパーク", "子供", "レゴ"], "photo_keyword": "legoland japan nagoya",
         "official_url": "https://www.legoland.jp/",
         "schedule_items": [
             {"time": "10:00", "place": "レゴランド・ジャパン MINILAND", "activity": "日本の都市をレゴで再現したエリアを見学"},
             {"time": "12:00", "place": "レゴランド レストラン", "activity": "ランチ"},
             {"time": "14:00", "place": "レゴランド アトラクションエリア", "activity": "人気アトラクションを体験"},
         ]},
    ],
    "兵庫県": [
        {"name": "姫路城", "type": "歴史・観光", "category": "outdoor",
         "weather_tags": ["sunny", "cloud"], "rain_ok": False,
         "area": "姫路市", "access": "姫路駅から徒歩15分",
         "hours": "9:00〜17:00", "rating": 4.7, "stay_time": "2〜3時間",
         "highlight": "世界遺産・国宝の白鷺城。日本で最も美しい城と称される完全な現存天守",
         "description": "1609年建築の国宝で、ユネスコ世界遺産。日本で最も原形をとどめる城。",
         "sns_reason": "白亜の姫路城と桜・青空のコントラストがInstagramで日本の城投稿トップクラス",
         "recommended_time": "桜の季節（4月上旬）の午前中",
         "tags": ["世界遺産", "城", "歴史"], "photo_keyword": "himeji castle japan",
         "official_url": "https://www.city.himeji.lg.jp/guide/castle/",
         "schedule_items": [
             {"time": "9:00", "place": "姫路城 大天守", "activity": "国宝天守を見学"},
             {"time": "11:00", "place": "好古園", "activity": "城郭を背景にした日本庭園散策"},
             {"time": "12:30", "place": "姫路駅前 えきそば", "activity": "姫路名物えきそばランチ"},
         ]},
        {"name": "神戸ハーバーランド umie", "type": "ショッピング", "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"], "rain_ok": True,
         "area": "神戸市中央区", "access": "ハーバーランド駅から徒歩3分",
         "hours": "10:00〜21:00", "rating": 4.2, "stay_time": "2〜3時間",
         "highlight": "神戸港とメリケンパークが目前に広がるウォーターフロントの複合施設",
         "description": "神戸港を望むショッピングモール。夜はポートタワーとのコラボが幻想的。",
         "sns_reason": "神戸ポートタワーと夜景の組み合わせがInstagramで神戸恋人スポットとして人気",
         "recommended_time": "夕方〜夜の神戸ポートタワーライトアップ時間帯",
         "tags": ["ショッピング", "神戸", "夜景"], "photo_keyword": "kobe harbor land night",
         "official_url": "https://umie.jp/",
         "schedule_items": [
             {"time": "16:00", "place": "神戸ハーバーランド umie", "activity": "ショッピング"},
             {"time": "18:00", "place": "メリケンパーク", "activity": "神戸港の夕日を鑑賞"},
             {"time": "19:30", "place": "南京町 中華街", "activity": "神戸中華街でディナー"},
         ]},
    ],
}

# 全都道府県対応のデフォルトスポット（DBにない場合）
_REGION_DEFAULTS: dict[str, list[str]] = {
    "東北": ["青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県"],
    "関東": ["茨城県", "栃木県", "群馬県", "埼玉県", "千葉県"],
    "中部": ["新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県", "三重県"],
    "近畿": ["滋賀県", "奈良県", "和歌山県"],
    "中国": ["鳥取県", "島根県", "岡山県", "広島県", "山口県"],
    "四国": ["徳島県", "香川県", "愛媛県", "高知県"],
    "九州": ["佐賀県", "大分県", "熊本県", "宮崎県", "鹿児島県"],
}

_PREF_NOTABLE: dict[str, list[dict]] = {
    "宮城県": [
        {"name": "松島海岸", "type": "自然・観光", "area": "松島町",
         "highlight": "日本三景のひとつ。260余の島々が浮かぶ絶景の多島海",
         "tags": ["自然", "日本三景", "絶景"], "photo_keyword": "matsushima bay japan"},
        {"name": "仙台うみの杜水族館", "type": "水族館", "area": "宮城野区",
         "highlight": "イワシの大群回遊が圧巻の東北最大の水族館",
         "tags": ["水族館", "仙台", "家族"], "photo_keyword": "sendai aquarium"},
    ],
    "広島県": [
        {"name": "厳島神社", "type": "神社仏閣", "area": "廿日市市宮島",
         "highlight": "海に浮かぶ大鳥居が神秘的な世界遺産・日本三景",
         "tags": ["世界遺産", "鳥居", "絶景"], "photo_keyword": "miyajima torii gate hiroshima"},
        {"name": "広島平和記念公園", "type": "歴史・観光", "area": "中区",
         "highlight": "原爆ドームと平和の灯が訴える歴史と平和のメッセージ",
         "tags": ["歴史", "世界遺産", "平和"], "photo_keyword": "hiroshima peace memorial"},
    ],
    "石川県": [
        {"name": "兼六園", "type": "公園・庭園", "area": "金沢市",
         "highlight": "日本三名園のひとつ。四季折々の美しさで知られる大名庭園",
         "tags": ["庭園", "金沢", "日本三名園"], "photo_keyword": "kenroku-en garden kanazawa"},
        {"name": "金沢21世紀美術館", "type": "美術館", "area": "金沢市",
         "highlight": "円形の建物と自然光を活かした革新的な現代アート空間",
         "tags": ["アート", "金沢", "SNS映え"], "photo_keyword": "kanazawa 21st century museum"},
    ],
    "静岡県": [
        {"name": "富士山世界遺産センター", "type": "博物館", "area": "富士宮市",
         "highlight": "日本一の山・富士山を科学と文化で深く知る体験型施設",
         "tags": ["富士山", "世界遺産", "博物館"], "photo_keyword": "mt fuji world heritage center"},
        {"name": "熱海温泉街・MOA美術館", "type": "観光・美術館", "area": "熱海市",
         "highlight": "温泉と国宝・重文を擁する眺望抜群の美術館が共存する温泉リゾート",
         "tags": ["温泉", "美術館", "熱海"], "photo_keyword": "atami moa museum japan"},
    ],
    "奈良県": [
        {"name": "東大寺・奈良の鹿", "type": "神社仏閣", "area": "奈良市",
         "highlight": "世界最大の木造建築・大仏殿と自由に歩く奈良の鹿が世界的に有名",
         "tags": ["世界遺産", "大仏", "鹿"], "photo_keyword": "nara todaiji deer japan"},
    ],
    "鹿児島県": [
        {"name": "桜島フェリーターミナル・桜島", "type": "自然・観光", "area": "鹿児島市",
         "highlight": "活火山・桜島と錦江湾の絶景。フェリーで15分の大自然アドベンチャー",
         "tags": ["火山", "絶景", "自然"], "photo_keyword": "sakurajima volcano kagoshima"},
    ],
    "熊本県": [
        {"name": "熊本城", "type": "歴史・観光", "area": "熊本市中央区",
         "highlight": "日本三名城のひとつ。震災から復興した難攻不落の名城",
         "tags": ["城", "歴史", "熊本"], "photo_keyword": "kumamoto castle japan"},
        {"name": "阿蘇山・草千里ヶ浜", "type": "自然・観光", "area": "阿蘇市",
         "highlight": "世界最大級のカルデラと広大な草原が広がる雄大な火山景観",
         "tags": ["自然", "火山", "絶景"], "photo_keyword": "aso volcano kumamoto"},
    ],
    "大分県": [
        {"name": "うみたまご（大分マリーンパレス水族館）", "type": "水族館", "area": "大分市",
         "rain_ok": True, "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"],
         "highlight": "セイウチや海獣のパフォーマンスで有名な大分の人気水族館",
         "tags": ["水族館", "大分", "家族"], "photo_keyword": "umitamago oita aquarium",
         "schedule_items": [{"time": "10:00", "place": "うみたまご", "activity": "海獣パフォーマンスを観覧"},
                             {"time": "12:00", "place": "大分市内", "activity": "ランチ"}]},
        {"name": "別府温泉・地獄めぐり", "type": "温泉・観光", "area": "別府市",
         "rain_ok": True, "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"],
         "highlight": "日本一の温泉湧出量を誇る別府の地獄8か所を巡る定番観光コース",
         "access": "別府駅から車で10分",
         "hours": "8:00〜17:00", "rating": 4.3, "stay_time": "2〜3時間",
         "sns_reason": "海地獄のコバルトブルーがInstagramで映えると話題",
         "tags": ["温泉", "別府", "観光"], "photo_keyword": "beppu hell onsen",
         "schedule_items": [{"time": "09:00", "place": "海地獄", "activity": "コバルトブルーの温泉を鑑賞・撮影"},
                             {"time": "10:30", "place": "血の池地獄", "activity": "赤い温泉を見学"},
                             {"time": "12:00", "place": "別府市内", "activity": "ランチ"}]},
        {"name": "高崎山自然動物園", "type": "動物園", "area": "大分市",
         "rain_ok": False, "category": "outdoor",
         "weather_tags": ["sunny", "cloud"],
         "highlight": "野生ニホンザルが1500頭以上生息する世界でも珍しい自然動物園",
         "tags": ["動物", "猿", "自然"], "photo_keyword": "takasakiyama monkey park oita",
         "schedule_items": [{"time": "10:00", "place": "高崎山自然動物園", "activity": "野生ザルの群れを観察"}]},
        {"name": "城島高原パーク", "type": "テーマパーク", "area": "別府市",
         "rain_ok": False, "category": "outdoor",
         "weather_tags": ["sunny", "cloud"],
         "highlight": "九州最大級の木製コースターを有する、別府の高原リゾートテーマパーク",
         "tags": ["テーマパーク", "アトラクション", "大分"], "photo_keyword": "kijima kogen park oita",
         "schedule_items": [{"time": "10:00", "place": "城島高原パーク", "activity": "アトラクションを満喫"}]},
        {"name": "由布院温泉・湯の坪街道", "type": "温泉・観光", "area": "由布市",
         "rain_ok": True, "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"],
         "highlight": "霧に包まれた由布岳と温泉街がSNSで常に人気の湯布院の中心街",
         "access": "由布院駅から徒歩5分",
         "hours": "終日（各店舗は10:00〜17:00頃）", "rating": 4.5, "stay_time": "2〜3時間",
         "sns_reason": "湯の坪街道の和スイーツと由布岳の組み合わせがInstagramで大分旅行の定番投稿",
         "tags": ["温泉", "湯布院", "食べ歩き"], "photo_keyword": "yufuin onsen street oita",
         "schedule_items": [{"time": "10:00", "place": "湯の坪街道", "activity": "由布院グルメ・スイーツ食べ歩き"},
                             {"time": "12:00", "place": "由布院 田舎料理", "activity": "郷土料理ランチ"},
                             {"time": "14:00", "place": "由布院温泉 日帰り湯", "activity": "日帰り温泉でリフレッシュ"}]},
        {"name": "別府ロープウェイ・鶴見岳", "type": "展望台", "area": "別府市",
         "rain_ok": False, "category": "outdoor",
         "weather_tags": ["sunny", "cloud"],
         "highlight": "標高1375mの鶴見岳山頂から別府湾と国東半島を一望できる絶景スポット",
         "access": "別府駅からバスで30分",
         "hours": "9:00〜17:00", "rating": 4.3, "stay_time": "1〜2時間",
         "sns_reason": "山頂から望む別府湾の雲海がInstagramで絶景写真として人気",
         "tags": ["展望台", "絶景", "大分"], "photo_keyword": "beppu ropeway tsurumi mountain",
         "schedule_items": [{"time": "10:00", "place": "別府ロープウェイ山麓駅", "activity": "ロープウェイで山頂へ"},
                             {"time": "10:30", "place": "鶴見岳山頂", "activity": "別府湾パノラマ撮影"}]},
        {"name": "臼杵石仏（臼杵磨崖仏）", "type": "歴史・観光", "area": "臼杵市",
         "rain_ok": True, "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"],
         "highlight": "平安後期に彫られた国宝の磨崖仏群。大分が誇る神秘的な文化遺産",
         "access": "臼杵駅から徒歩15分",
         "hours": "8:30〜17:00", "rating": 4.4, "stay_time": "1〜2時間",
         "sns_reason": "苔むした岩に刻まれた神秘的な仏像がフォトジェニックとSNSで話題",
         "tags": ["歴史", "国宝", "大分"], "photo_keyword": "usuki stone buddha oita",
         "schedule_items": [{"time": "10:00", "place": "臼杵石仏", "activity": "国宝磨崖仏を見学・撮影"}]},
        {"name": "やせうま・とり天 発祥の地 大分市内グルメ巡り", "type": "グルメ", "area": "大分市",
         "rain_ok": True, "category": "indoor",
         "weather_tags": ["sunny", "cloud", "rain"],
         "highlight": "とり天・やせうまなど大分B級グルメの聖地。地元民も通う名店が集まる",
         "access": "大分駅周辺",
         "hours": "11:00〜22:00（店舗により異なる）", "rating": 4.3, "stay_time": "1〜2時間",
         "sns_reason": "とり天発祥の店がTikTokとInstagramで大分グルメ旅の定番スポットとして紹介",
         "tags": ["グルメ", "大分", "B級グルメ"], "photo_keyword": "oita toriten chicken tempura",
         "schedule_items": [{"time": "12:00", "place": "大分市内 とり天の名店", "activity": "とり天ランチ"},
                             {"time": "14:00", "place": "大分市中心街", "activity": "大分グルメ食べ歩き"}]},
        {"name": "九重夢大吊橋", "type": "自然・観光", "area": "玖珠郡九重町",
         "rain_ok": False, "category": "outdoor",
         "weather_tags": ["sunny", "cloud"],
         "highlight": "歩行者専用橋として日本一の高さ173mを誇る大吊橋。眼下に滝と渓谷の絶景",
         "access": "九重ICから車で15分",
         "hours": "8:30〜17:00", "rating": 4.5, "stay_time": "1〜2時間",
         "sns_reason": "紅葉シーズンの渓谷と吊橋の写真がSNSで大分絶景スポットとして毎年話題",
         "tags": ["絶景", "橋", "自然"], "photo_keyword": "kokonoe dream bridge oita",
         "schedule_items": [{"time": "10:00", "place": "九重夢大吊橋", "activity": "日本一の高さの橋から渓谷絶景を撮影"}]},
        {"name": "中津からあげ 聖地・中津市", "type": "グルメ", "area": "中津市",
         "rain_ok": True, "category": "outdoor",
         "weather_tags": ["sunny", "cloud", "rain"],
         "highlight": "全国からあげファンが巡礼する「からあげの聖地」。数十店舗のからあげ専門店が軒を連ねる",
         "access": "中津駅から徒歩・車で各店舗へ",
         "hours": "10:00〜20:00（店舗による）", "rating": 4.5, "stay_time": "1〜2時間",
         "sns_reason": "TikTokで「からあげの聖地」として何百万回も再生された中津からあげ食べ歩き動画",
         "tags": ["グルメ", "からあげ", "食べ歩き"], "photo_keyword": "nakatsu karaage fried chicken oita",
         "schedule_items": [{"time": "11:00", "place": "中津 からあげ 聖地", "activity": "複数店舗でからあげ食べ比べ"}]},
    ],
    "宮崎県": [
        {"name": "高千穂峡", "type": "自然・観光", "area": "西臼杵郡高千穂町",
         "highlight": "柱状節理の断崖と滝が織りなす神話の国・宮崎の絶景渓谷",
         "tags": ["自然", "絶景", "神話"], "photo_keyword": "takachiho gorge miyazaki"},
        {"name": "青島神社・青島ビーチ", "type": "神社仏閣", "area": "宮崎市",
         "highlight": "サンゴ礁に浮かぶ小島の神社とサーファーに人気の南国ビーチ",
         "tags": ["神社", "ビーチ", "絶景"], "photo_keyword": "aoshima shrine miyazaki beach"},
    ],
    "岩手県": [
        {"name": "中尊寺・金色堂", "type": "神社仏閣", "area": "西磐井郡平泉町",
         "highlight": "黄金に輝く金色堂が圧倒的な世界遺産・平泉の中心寺院",
         "tags": ["世界遺産", "歴史", "金色堂"], "photo_keyword": "chusonji golden hall iwate"},
    ],
    "青森県": [
        {"name": "弘前城・弘前公園", "type": "歴史・観光", "area": "弘前市",
         "highlight": "日本で最も美しい桜の名所の一つで重要文化財の天守が現存",
         "tags": ["城", "桜", "歴史"], "photo_keyword": "hirosaki castle cherry blossom"},
        {"name": "奥入瀬渓流", "type": "自然・観光", "area": "十和田市",
         "highlight": "苔むした岩と清流が作り出す日本有数の渓流美で紅葉が特に有名",
         "tags": ["自然", "渓流", "紅葉"], "photo_keyword": "oirase stream aomori"},
    ],
    "秋田県": [
        {"name": "男鹿水族館GAO", "type": "水族館", "area": "男鹿市",
         "highlight": "日本海の生き物とホッキョクグマが見られる秋田の人気水族館",
         "tags": ["水族館", "秋田", "ホッキョクグマ"], "photo_keyword": "oga aquarium gao akita"},
    ],
    "山形県": [
        {"name": "山形県立博物館・最上川", "type": "自然・観光", "area": "山形市",
         "highlight": "日本三大急流のひとつ最上川と山形の歴史文化を楽しめる",
         "tags": ["自然", "川", "観光"], "photo_keyword": "mogami river yamagata"},
    ],
    "茨城県": [
        {"name": "ひたち海浜公園", "type": "公園・庭園", "area": "ひたちなか市",
         "highlight": "ネモフィラの丘とコキアの丘がSNSで毎年バイラルになる絶景公園",
         "tags": ["公園", "ネモフィラ", "絶景"], "photo_keyword": "hitachi seaside park nemophila"},
    ],
    "栃木県": [
        {"name": "日光東照宮", "type": "神社仏閣", "area": "日光市",
         "highlight": "徳川家康を祀る華麗な彫刻と国宝建築が集積する世界遺産",
         "tags": ["世界遺産", "歴史", "日光"], "photo_keyword": "nikko toshogu shrine"},
        {"name": "那須どうぶつ王国", "type": "動物園", "area": "那須郡那須町",
         "highlight": "スナドリネコやバードショーなど希少動物が間近で見られる動物公園",
         "tags": ["動物", "那須", "家族"], "photo_keyword": "nasu animal kingdom tochigi"},
    ],
    "群馬県": [
        {"name": "草津温泉・湯畑", "type": "温泉・観光", "area": "吾妻郡草津町",
         "highlight": "日本三大名泉の湯畑を中心とした温泉街がSNSで人気の定番観光地",
         "tags": ["温泉", "草津", "絶景"], "photo_keyword": "kusatsu onsen yubatake gunma"},
    ],
    "新潟県": [
        {"name": "佐渡島・佐渡金山", "type": "歴史・観光", "area": "佐渡市",
         "highlight": "世界最大級の金銀山遺跡と朱鷺の生息地として知られる離島",
         "tags": ["歴史", "世界遺産候補", "島"], "photo_keyword": "sado gold mine niigata"},
    ],
    "長野県": [
        {"name": "善光寺", "type": "神社仏閣", "area": "長野市",
         "highlight": "1400年の歴史を持つ無宗派の古刹。お戒壇巡りが体験できる",
         "tags": ["寺院", "歴史", "参拝"], "photo_keyword": "zenkoji temple nagano"},
        {"name": "上高地", "type": "自然・観光", "area": "松本市",
         "highlight": "北アルプスの麓に広がる日本有数の山岳リゾート",
         "tags": ["自然", "山岳", "絶景"], "photo_keyword": "kamikochi nagano alps"},
    ],
    "岐阜県": [
        {"name": "白川郷・合掌造り集落", "type": "歴史・観光", "area": "大野郡白川村",
         "highlight": "雪化粧した合掌造りの集落が世界遺産に登録された絶景の農村",
         "tags": ["世界遺産", "合掌造り", "雪景色"], "photo_keyword": "shirakawago gassho zukuri"},
    ],
    "山梨県": [
        {"name": "河口湖・富士山眺望スポット", "type": "自然・観光", "area": "南都留郡富士河口湖町",
         "highlight": "富士山を湖面に映す逆さ富士がInstagramで世界中から撮影される絶景",
         "tags": ["富士山", "絶景", "湖"], "photo_keyword": "kawaguchiko lake fuji reflection"},
    ],
    "滋賀県": [
        {"name": "彦根城", "type": "歴史・観光", "area": "彦根市",
         "highlight": "現存天守を有する国宝の城。ひこにゃんが人気のマスコット",
         "tags": ["城", "国宝", "歴史"], "photo_keyword": "hikone castle shiga"},
    ],
    "和歌山県": [
        {"name": "熊野古道・那智滝", "type": "自然・観光", "area": "東牟婁郡那智勝浦町",
         "highlight": "日本一の落差を誇る那智滝と世界遺産の熊野古道が融合する聖地",
         "tags": ["世界遺産", "滝", "聖地"], "photo_keyword": "nachi falls kumano wakayama"},
    ],
    "鳥取県": [
        {"name": "鳥取砂丘", "type": "自然・観光", "area": "鳥取市",
         "highlight": "日本最大の砂丘。ラクダ乗りとパラグライダーが体験できる",
         "tags": ["自然", "砂丘", "絶景"], "photo_keyword": "tottori sand dunes"},
    ],
    "島根県": [
        {"name": "出雲大社", "type": "神社仏閣", "area": "出雲市",
         "highlight": "縁結びの神・大国主大神を祀る日本最古の神社のひとつ",
         "tags": ["神社", "縁結び", "歴史"], "photo_keyword": "izumo taisha shrine"},
    ],
    "岡山県": [
        {"name": "倉敷美観地区", "type": "歴史・観光", "area": "倉敷市",
         "highlight": "江戸時代の白壁土蔵と柳並木が残る大原美術館のある風情ある街並み",
         "tags": ["歴史", "美観", "アート"], "photo_keyword": "kurashiki bikan district"},
    ],
    "山口県": [
        {"name": "秋芳洞", "type": "自然・観光", "area": "美祢市",
         "highlight": "日本最大の鍾乳洞。黄金柱や百枚皿など絶景の地底世界",
         "tags": ["鍾乳洞", "自然", "絶景"], "photo_keyword": "akiyoshido cave yamaguchi"},
    ],
    "徳島県": [
        {"name": "大塚国際美術館", "type": "美術館", "category": "indoor",
         "area": "鳴門市",
         "highlight": "システィーナ礼拝堂など世界の名画を実物大で再現した世界最大級の陶板名画美術館",
         "tags": ["美術館", "アート", "SNS映え"], "photo_keyword": "otsuka art museum tokushima"},
    ],
    "香川県": [
        {"name": "直島・地中美術館", "type": "美術館", "area": "香川郡直島町",
         "highlight": "地中に埋め込まれたモネの睡蓮が見られる安藤忠雄設計の美術館",
         "tags": ["アート", "美術館", "離島"], "photo_keyword": "naoshima art museum kagawa"},
    ],
    "愛媛県": [
        {"name": "道後温泉本館", "type": "温泉・観光", "area": "松山市",
         "highlight": "日本最古の温泉のひとつ。千と千尋の神隠しのモデルとも言われる明治建築",
         "tags": ["温泉", "歴史", "建築"], "photo_keyword": "dogo onsen matsuyama ehime"},
    ],
    "高知県": [
        {"name": "桂浜", "type": "自然・観光", "area": "高知市",
         "highlight": "坂本龍馬像が立つ月の名所として知られる太平洋の弓状の砂浜",
         "tags": ["海岸", "龍馬", "絶景"], "photo_keyword": "katsurahama beach kochi"},
    ],
    "佐賀県": [
        {"name": "嬉野温泉", "type": "温泉・観光", "area": "嬉野市",
         "highlight": "美肌の湯として有名な日本三大美肌の湯のひとつ",
         "tags": ["温泉", "美肌", "佐賀"], "photo_keyword": "ureshino onsen saga"},
    ],
}


def _get_spots_for_prefecture(place: str) -> list[dict]:
    """Return real spot list for the given prefecture."""
    if place in _SPOTS_DB:
        return _SPOTS_DB[place]

    # Try notable spots for prefectures with partial data
    if place in _PREF_NOTABLE:
        return _expand_notable(place, _PREF_NOTABLE[place])

    # For unmapped prefectures, return generic but named alternatives
    return _generic_named_spots(place)


def _expand_notable(place: str, notable: list[dict]) -> list[dict]:
    """Expand minimal notable spot entries into full format."""
    expanded = []
    for s in notable:
        name = s["name"]
        area = s.get("area", place)
        tags = s.get("tags", ["観光", "おでかけ"])
        photo_kw = s.get("photo_keyword", "japan tourism")
        plan_type = s.get("type", "観光")
        expanded.append({
            "name": name,
            "type": plan_type,
            "category": "outdoor",
            "weather_tags": ["sunny", "cloud"],
            "rain_ok": False,
            "area": area,
            "access": f"{place}から電車・バスでアクセス",
            "hours": "9:00〜17:00",
            "rating": 4.3,
            "stay_time": "1〜2時間",
            "highlight": s.get("highlight", f"{name}は{place}を代表する人気観光地"),
            "description": f"{name}は{place}屈指の観光スポット。地元でも人気の必訪スポット。",
            "sns_reason": f"{name}の絶景・体験がInstagramで{place}旅行の定番投稿になっている",
            "recommended_time": "午前中〜昼がおすすめ",
            "tags": tags,
            "photo_keyword": photo_kw,
            "official_url": "",
            "schedule_items": [
                {"time": "10:00", "place": name, "activity": "見学・撮影"},
                {"time": "12:00", "place": f"{place}の地元レストラン", "activity": "ご当地グルメランチ"},
                {"time": "14:00", "place": name, "activity": "周辺散策"},
            ],
        })
    return expanded


def _generic_named_spots(place: str) -> list[dict]:
    """Fallback for unmapped prefectures — uses prefecture-specific naming."""
    pref = place.replace("県", "").replace("府", "").replace("都", "").replace("道", "")
    spots = [
        {"name": f"{pref}県立博物館", "type": "博物館", "category": "indoor",
         "tags": ["博物館", "文化", "屋内"], "photo_keyword": f"{pref} museum japan"},
        {"name": f"{pref}城", "type": "城", "category": "outdoor",
         "tags": ["城", "歴史", "観光"], "photo_keyword": f"{pref} castle japan"},
        {"name": f"{pref}市美術館", "type": "美術館", "category": "indoor",
         "tags": ["美術館", "アート", "屋内"], "photo_keyword": f"japan art museum"},
    ]
    result = []
    for s in spots:
        name = s["name"]
        result.append({
            "name": name,
            "type": s["type"],
            "category": s["category"],
            "weather_tags": ["sunny", "cloud", "rain"] if s["category"] == "indoor" else ["sunny", "cloud"],
            "rain_ok": s["category"] == "indoor",
            "area": f"{place}中心部",
            "address": f"{place}中心部",   # ← prefecture is explicit
            "access": f"{pref}駅からアクセス可能",
            "hours": "9:00〜17:00",
            "rating": 4.0,
            "stay_time": "1〜2時間",
            "highlight": f"{place}を代表する施設",
            "description": f"{name}は{place}の定番観光スポット。地元の人にも人気。",
            "sns_reason": f"SNSで{place}旅行の定番スポットとして人気",
            "recommended_time": "午前中がおすすめ",
            "tags": s["tags"],
            "photo_keyword": s["photo_keyword"],
            "official_url": "",
            "schedule_items": [
                {"time": "10:00", "place": name, "activity": "見学・観覧"},
                {"time": "12:00", "place": f"{pref}名物レストラン", "activity": "ご当地グルメランチ"},
            ],
        })
    return result


# ── Fallback ──────────────────────────────────────────────────────────────────

# Preferred spot types per companion type
_PEOPLE_TYPE_PREF: dict[str, set[str]] = {
    "カップル": {"展望台", "温泉・観光", "自然・観光", "体験施設", "グルメ", "カフェ",
                 "観光・ショッピング", "歴史・観光", "公園・庭園"},
    "家族":     {"動物園", "テーマパーク", "水族館", "体験施設", "公園・庭園",
                 "博物館", "自然・観光"},
    "友達":     {"グルメ", "グルメ・観光", "体験施設", "ショッピング",
                 "観光・ショッピング", "温泉・観光", "自然・観光"},
    "ひとり":   {"美術館", "博物館", "神社仏閣", "温泉・観光", "自然・観光",
                 "観光", "歴史・観光", "公園・庭園"},
}

# Preferred spot types per purpose
_PURPOSE_TYPE_PREF: dict[str, set[str]] = {
    "デート":     {"展望台", "温泉・観光", "自然・観光", "体験施設", "カフェ"},
    "SNS映え":    {"展望台", "自然・観光", "公園・庭園", "体験施設", "神社仏閣"},
    "食べ歩き":   {"グルメ", "グルメ・観光", "観光・ショッピング"},
    "自然":       {"自然・観光", "公園・庭園", "温泉・観光"},
    "ゆっくり":   {"温泉・観光", "美術館", "博物館", "神社仏閣", "公園・庭園"},
}

# Minimum hours required per spot type
_TYPE_MIN_HOURS: dict[str, float] = {
    "テーマパーク":         5.0,
    "動物園":               3.0,
    "水族館":               2.0,
    "博物館":               1.5,
    "美術館":               1.5,
    "体験施設":             1.5,
    "神社仏閣":             1.0,
    "歴史・観光":           1.5,
    "自然・観光":           1.5,
    "公園・庭園":           1.5,
    "ショッピング":         2.0,
    "観光・ショッピング":   1.5,
    "グルメ":               1.0,
    "カフェ":               1.0,
    "グルメ・観光":         1.5,
    "展望台":               1.0,
    "城":                   1.5,
}


def _spot_min_hours(spot: dict) -> float:
    return _TYPE_MIN_HOURS.get(spot.get("type", "スポット"), 1.5)


def _shift_schedule(items: list[dict], start_time: str, end_time: str) -> list[dict]:
    """Shift schedule to start_time; compute/shift end_time per item; drop items past end_time."""
    if not items:
        return []
    try:
        fh, fm = map(int, items[0]["time"].split(":"))
        sh, sm = map(int, start_time.split(":"))
        eh, em = map(int, end_time.split(":"))
    except (ValueError, IndexError, KeyError):
        return items

    shift   = (sh * 60 + sm) - (fh * 60 + fm)
    end_min = eh * 60 + em
    shifted = []

    for item in items:
        try:
            h, m    = map(int, item["time"].split(":"))
            new_min = h * 60 + m + shift
            if new_min >= end_min:
                break
            new_item = dict(item)
            new_item["time"] = f"{new_min // 60:02d}:{new_min % 60:02d}"

            # Shift end_time if present
            if item.get("end_time"):
                try:
                    eh2, em2 = map(int, item["end_time"].split(":"))
                    et_min   = eh2 * 60 + em2 + shift
                    et_min   = min(et_min, end_min)
                    new_item["end_time"] = f"{et_min // 60:02d}:{et_min % 60:02d}"
                except (ValueError, IndexError):
                    new_item["end_time"] = ""

            shifted.append(new_item)
        except (ValueError, IndexError, KeyError):
            shifted.append(dict(item))

    # Back-fill end_times from next item's time when missing
    for i, s in enumerate(shifted):
        if not s.get("end_time"):
            mv = int(s.get("travel_min") or 0)
            if i + 1 < len(shifted):
                nxt_h, nxt_m = map(int, shifted[i + 1]["time"].split(":"))
                et_min = nxt_h * 60 + nxt_m - mv
            else:
                et_min = end_min
            if et_min > 0:
                s["end_time"] = f"{et_min // 60:02d}:{et_min % 60:02d}"

    # Ensure last item's end_time does not exceed user end_time
    if shifted and shifted[-1].get("end_time"):
        try:
            lh, lm = map(int, shifted[-1]["end_time"].split(":"))
            if lh * 60 + lm > end_min:
                shifted[-1]["end_time"] = end_time
        except (ValueError, IndexError):
            shifted[-1]["end_time"] = end_time

    return shifted


def fallback_recommendations(search: dict[str, str], weather: dict[str, Any]) -> list[dict[str, Any]]:
    place       = search.get("place") or "東京都"
    budget      = search.get("budget") or "気にしない"
    start_time  = search.get("start_time") or "10:00"
    end_time    = search.get("end_time")   or "17:00"
    people      = search.get("people", "")
    purpose     = search.get("purpose", "")
    duration    = calc_duration(start_time, end_time)
    rec         = weather.get("recommendation") or "屋内と屋外の両方"
    indoor_pref = "屋内" in rec

    all_spots = _get_spots_for_prefecture(place)

    # Filter: keep only spots that fit in available time
    time_ok = [s for s in all_spots if _spot_min_hours(s) <= max(duration, 1.0)]

    # Split by weather preference
    if indoor_pref:
        weather_pref  = [s for s in time_ok if s.get("rain_ok")]
        weather_other = [s for s in time_ok if not s.get("rain_ok")]
    else:
        weather_pref  = [s for s in time_ok if not s.get("rain_ok")]
        weather_other = [s for s in time_ok if s.get("rain_ok")]

    random.shuffle(weather_pref)
    random.shuffle(weather_other)
    weather_ordered = weather_pref + weather_other

    # Re-order by people/purpose preference so conditions actually affect results
    preferred_types = (
        _PEOPLE_TYPE_PREF.get(people, set()) | _PURPOSE_TYPE_PREF.get(purpose, set())
    )
    if preferred_types:
        pref_spots  = [s for s in weather_ordered if s.get("type") in preferred_types]
        other_spots = [s for s in weather_ordered if s.get("type") not in preferred_types]
        ordered = (pref_spots + other_spots)[:10]
    else:
        ordered = weather_ordered[:10]

    # Pad with same-prefecture generic spots if fewer than 10 (never use other prefectures)
    if len(ordered) < 10:
        generic = _generic_named_spots(place)
        generic_ok = [s for s in generic if s not in ordered]
        ordered = (ordered + generic_ok)[:10]

    # グループ化して 1日コースを生成
    themes = ["おすすめ1日コース", "観光・グルメコース", "体験・穴場コース"]
    chunk  = 6
    result = []
    for i, theme in enumerate(themes):
        chunk_spots = ordered[i * chunk:(i + 1) * chunk]
        if not chunk_spots:
            break
        names = [s["name"] for s in chunk_spots]
        plan  = _build_day_plan({"plan_title": theme, "spots": names}, ordered, search, weather)
        if plan["spots"]:
            result.append(plan)

    if not result:
        all_names = [s["name"] for s in ordered[:7]]
        plan = _build_day_plan({"plan_title": "おすすめコース", "spots": all_names}, ordered, search, weather)
        if plan["spots"]:
            result.append(plan)

    logger.info("[Fallback] %d コース生成", len(result))
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def plan_id(name: str, area: str) -> str:
    return sha1(f"{area}:{name}".encode()).hexdigest()[:12]


def build_links(
    name: str,
    map_query: str,
    instagram_tag: str,
    official_url: str,
    address: str = "",
) -> dict[str, str]:
    encoded_map  = quote_plus(map_query or name)
    encoded_name = quote_plus(name)
    encoded_addr = quote_plus(address or map_query or name)
    ig_tag       = quote((instagram_tag or name).replace(" ", "").replace("　", ""), safe="")
    return {
        "map":        f"https://www.google.com/maps/search/?api=1&query={encoded_map}",
        "directions": f"https://www.google.com/maps/dir/?api=1&destination={encoded_addr}",
        "hotpepper":  f"https://www.hotpepper.jp/CSP/psh010/doBasic?keyword={encoded_name}",
        "instagram":  f"https://www.instagram.com/explore/tags/{ig_tag}/",
        "tiktok":     f"https://www.tiktok.com/search?q={encoded_name}",
        "official":   official_url or "",
    }

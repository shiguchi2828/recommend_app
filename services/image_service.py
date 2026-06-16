"""
Real facility photo service — prioritizes visually appealing, landscape-oriented
photos of the actual facility.

Priority:
  1. Pre-curated high-quality Wikimedia photos for known facilities
  2. Google Places API            (GOOGLE_API_KEY required)
  3. Wikipedia REST API           (free, no key)
  4. Wikimedia Commons search     (free, landscape filter)
  5. Unsplash keyword search      (UNSPLASH_ACCESS_KEY required)
  6. Category-based Unsplash      (always available)
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import requests

from utils.plan_store import load as store_load, save as store_save

# ── Constants ─────────────────────────────────────────────────────────────────

_WIKI_SUMMARY  = "https://ja.wikipedia.org/api/rest_v1/page/summary/{}"
_WIKI_API      = "https://ja.wikipedia.org/w/api.php"
_COMMONS_API   = "https://commons.wikimedia.org/w/api.php"
_PLACES_SEARCH = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
_PLACES_PHOTO  = "https://maps.googleapis.com/maps/api/place/photo"
_UNSPLASH_BASE = "https://images.unsplash.com/photo-"
_PARAMS        = "?auto=format&fit=crop&w=800&q=80"
_UA            = "OutingRecommendApp/2.0 (educational)"
_TIMEOUT       = 5

# ── Curated high-quality photos (Wikimedia Commons Special:FilePath) ──────────
# Format: facility name → Wikimedia Commons filename (stable URL)
# Special:FilePath redirects are permanent as long as the file exists on Commons

_WMC = "https://commons.wikimedia.org/wiki/Special:FilePath/"
_W   = "?width=800"

_KNOWN: dict[str, str] = {
    # 東京
    "東京スカイツリー":
        _WMC + "Tokyo_Skytree_from_Sumida_River.jpg" + _W,
    "浅草寺":
        _WMC + "Asakusa_Senso-ji_2019.jpg" + _W,
    "浅草寺・仲見世通り":
        _WMC + "Asakusa_Senso-ji_2019.jpg" + _W,
    "teamLab Planets TOKYO":
        _WMC + "TeamLab_Planets_TOKYO_DMM.jpg" + _W,
    "渋谷スクランブルスクエア展望施設 SHIBUYA SKY":
        _WMC + "Shibuya_Scramble_Square_201911.jpg" + _W,
    "上野動物園":
        _WMC + "Giant_panda_at_Ueno_Zoo.jpg" + _W,
    "お台場・ダイバーシティ東京":
        _WMC + "DiverCity_Tokyo_Plaza_Gundam.jpg" + _W,

    # 神奈川
    "横浜赤レンガ倉庫":
        _WMC + "Yokohama_Red_Brick_Warehouse.jpg" + _W,
    "新江ノ島水族館":
        _WMC + "Enoshima_Aquarium_Jellyfish.jpg" + _W,
    "鎌倉大仏殿高徳院":
        _WMC + "Kamakura_Budda_Daibutsu_front_1885.jpg" + _W,
    "横浜中華街":
        _WMC + "Yokohama_China_Town_Illumination.jpg" + _W,

    # 大阪
    "ユニバーサル・スタジオ・ジャパン":
        _WMC + "Universal_Studios_Japan_Globe.jpg" + _W,
    "道頓堀":
        _WMC + "Dotonbori_Glico_Sign_Osaka.jpg" + _W,
    "海遊館":
        _WMC + "Osaka_Aquarium_Kaiyukan_tank.jpg" + _W,
    "あべのハルカス展望台 ハルカス300":
        _WMC + "Abeno_Harukas_20131019.jpg" + _W,

    # 京都
    "伏見稲荷大社":
        _WMC + "Fushimi_Inari_Taisha_torii.jpg" + _W,
    "嵐山・竹林の小径":
        _WMC + "Arashiyama_Bamboo_grove_2019.jpg" + _W,
    "清水寺":
        _WMC + "Kiyomizu-dera_in_Kyoto_Japan.jpg" + _W,

    # 北海道
    "旭山動物園":
        _WMC + "Asahiyama_Zoo_Penguin_March.jpg" + _W,
    "函館山展望台":
        _WMC + "Hakodate_night_view_2014.jpg" + _W,
    "小樽運河":
        _WMC + "Otaru_Canal_at_night.jpg" + _W,

    # 沖縄
    "美ら海水族館":
        _WMC + "Okinawa_churaumi_aquarium.jpg" + _W,
    "首里城公園":
        _WMC + "Shuri_Castle_Naha_Okinawa.jpg" + _W,
    "国際通り":
        _WMC + "Kokusai_Street_Naha_Okinawa.jpg" + _W,

    # 長崎
    "ハウステンボス":
        _WMC + "Huis_Ten_Bosch_illumination.jpg" + _W,
    "稲佐山展望台":
        _WMC + "Nagasaki_night_view_Inasayama.jpg" + _W,
    "長崎バイオパーク":
        _WMC + "Nagasaki_Biopark_capybara.jpg" + _W,
    "出島":
        _WMC + "Dejima_Nagasaki.jpg" + _W,
    "アミュプラザ長崎":
        _WMC + "JR_Nagasaki_Station.jpg" + _W,

    # 福岡
    "太宰府天満宮":
        _WMC + "Dazaifu_Tenmangu_Shrine.jpg" + _W,
    "キャナルシティ博多":
        _WMC + "Canal_City_Hakata_Fountain.jpg" + _W,

    # 愛知
    "名古屋城":
        _WMC + "Nagoya_Castle_Main_Tower.jpg" + _W,
    "レゴランド・ジャパン・リゾート":
        _WMC + "Legoland_Japan_entrance.jpg" + _W,

    # 兵庫
    "姫路城":
        _WMC + "Himeji_Castle_The_White_Heron_Castle.jpg" + _W,
    "神戸ハーバーランド umie":
        _WMC + "Kobe_Harborland_at_night.jpg" + _W,

    # 奈良
    "東大寺・奈良の鹿":
        _WMC + "Todaiji_daibutsuden.jpg" + _W,

    # 大分
    "うみたまご（大分マリーンパレス水族館）":
        _WMC + "Umitamago_aquarium_Oita.jpg" + _W,
    "うみたまご":
        _WMC + "Umitamago_aquarium_Oita.jpg" + _W,
    "別府温泉・地獄めぐり":
        _WMC + "Beppu_jigoku_bozu.jpg" + _W,
    "高崎山自然動物園":
        _WMC + "Takasakiyama_natural_zoological_garden.jpg" + _W,
    "城島高原パーク":
        _WMC + "Kijima_Kogen_Park_roller_coaster.jpg" + _W,
    "由布院温泉":
        _WMC + "Yufuin_lake_Kinrin.jpg" + _W,

    # 熊本
    "熊本城":
        _WMC + "Kumamoto_Castle.jpg" + _W,
    "阿蘇山・草千里ヶ浜":
        _WMC + "Mount_Aso_crater.jpg" + _W,

    # 広島
    "厳島神社":
        _WMC + "Itsukushima_torii_gate_2019.jpg" + _W,
    "広島平和記念公園":
        _WMC + "A-Bomb_Dome_Hiroshima.jpg" + _W,

    # 石川
    "兼六園":
        _WMC + "Kenrokuen_Garden_Kanazawa.jpg" + _W,
    "金沢21世紀美術館":
        _WMC + "21st_Century_Museum_of_Contemporary_Art_Kanazawa.jpg" + _W,

    # 宮城
    "松島海岸":
        _WMC + "Matsushima_from_Oshima_Island.jpg" + _W,

    # 栃木
    "日光東照宮":
        _WMC + "Nikko_Toshogu_Yomeimon.jpg" + _W,

    # 群馬
    "草津温泉・湯畑":
        _WMC + "Kusatsu_onsen_yubatake.jpg" + _W,

    # 鳥取
    "鳥取砂丘":
        _WMC + "Tottori_Sand_Dunes_2010.jpg" + _W,

    # 島根
    "出雲大社":
        _WMC + "Izumo-taisha_Shimane_Japan.jpg" + _W,

    # 岡山
    "倉敷美観地区":
        _WMC + "Kurashiki_Bikan_historical_quarter.jpg" + _W,

    # 宮崎
    "高千穂峡":
        _WMC + "Takachiho_gorge_Miyazaki.jpg" + _W,

    # 山梨
    "河口湖・富士山眺望スポット":
        _WMC + "Mount_Fuji_from_Lake_Kawaguchi.jpg" + _W,

    # 長野
    "上高地":
        _WMC + "Kamikochi_Taisho_Pond.jpg" + _W,

    # 岐阜
    "白川郷・合掌造り集落":
        _WMC + "Shirakawa-go_gassho_zukuri.jpg" + _W,

    # 和歌山
    "熊野古道・那智滝":
        _WMC + "Nachi_Falls_Wakayama.jpg" + _W,

    # 愛媛
    "道後温泉本館":
        _WMC + "Dogo_Onsen_Honkan_2016.jpg" + _W,

    # 香川
    "直島・地中美術館":
        _WMC + "Chichu_Art_Museum_Naoshima.jpg" + _W,
}


# Curated fallback Unsplash IDs by category (last resort)
_CATEGORY_FALLBACK: list[tuple[list[str], str]] = [
    (["テーマパーク", "遊園地", "アトラクション"],  "1524985069026-dd778a71c7b4"),
    (["動物園", "動物", "ペンギン", "パンダ"],       "1564349683136-77e08dba1ef7"),
    (["水族館", "クラゲ", "イルカ"],                 "1518998053901-5348d3961a04"),
    (["温泉", "スパ"],                               "1555817128-d50573604c8a"),
    (["ショッピング", "モール", "百貨店"],            "1441984904996-e0b6ba687e04"),
    (["夜景", "展望台", "タワー", "イルミ"],          "1480714378408-67cf0d13bc1b"),
    (["グルメ", "レストラン", "食べ歩き"],            "1565299624946-b28f40a0ae38"),
    (["ビーチ", "海", "島", "砂浜"],                 "1507525428034-b723cf961d3e"),
    (["観光", "名所", "城", "神社", "寺", "歴史"],   "1528360983277-13d401cdc186"),
    (["自然", "山", "渓谷", "絶景"],                 "1526481280693-3bfa7568e0f3"),
    (["カフェ", "コーヒー"],                          "1495474472287-4d71bcdd2085"),
    (["公園", "緑", "ガーデン"],                     "1469474968028-56623f02e42e"),
    (["市場", "マーケット"],                          "1488459716781-31db52582fe9"),
    (["港", "みなとみらい"],                          "1541640019776-e6154e9d1651"),
]
_DEFAULT_FALLBACK = "1469474968028-56623f02e42e"


# ── Public API ─────────────────────────────────────────────────────────────────

def photo_url_for_spot(
    tags:       list[str],
    spot_type:  str,
    keyword_en: str = "",
    name:       str = "",
    place:      str = "",
) -> str:
    cache: dict[str, Any] = store_load("photo_cache") or {}
    cache_key = f"v3:{name or keyword_en or spot_type}"[:80]

    if cache_key in cache:
        return cache[cache_key]

    url = _resolve_photo(name, place, tags, spot_type, keyword_en)

    if url:
        cache[cache_key] = url
        store_save("photo_cache", cache)
    return url


def _resolve_photo(
    name: str, place: str, tags: list[str], spot_type: str, keyword_en: str
) -> str:
    # 1. Pre-curated Wikimedia photo (instant, high quality)
    if name:
        curated = _curated_lookup(name)
        if curated:
            live = _verify_url(curated)
            if live:
                return live

    # 2. Google Places API (real facility photo, best quality)
    google_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if google_key and name:
        url = _google_places_photo(name, place, google_key)
        if url:
            return url

    # 3. Wikipedia REST API (free, real photo)
    if name:
        url = _wikipedia_photo(name)
        if url:
            return url

    # 4. Wikimedia Commons search (landscape filter)
    if name:
        url = _commons_search(name)
        if url:
            return url

    # 5. Unsplash keyword search
    unsplash_key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    if unsplash_key and (keyword_en or name):
        url = _unsplash_search(unsplash_key, keyword_en or name)
        if url:
            return url

    # 6. Category-based fallback
    return _category_fallback(tags, spot_type)


# ── Curated lookup ────────────────────────────────────────────────────────────

def _curated_lookup(name: str) -> str:
    # Exact match
    if name in _KNOWN:
        return _KNOWN[name]
    # Partial match (strip parenthetical)
    clean = re.sub(r'[（(（][^）)）]*[）)）]', '', name).strip()
    if clean in _KNOWN:
        return _KNOWN[clean]
    # Check if any known key is a substring of name
    for key, url in _KNOWN.items():
        if key in name or name in key:
            return url
    return ""


def _verify_url(url: str) -> str:
    """Return url if it responds 200, else empty string (fast head request)."""
    try:
        r = requests.head(url, timeout=3, headers={"User-Agent": _UA},
                         allow_redirects=True)
        if r.status_code < 400:
            return url
    except Exception:
        pass
    return ""


# ── Google Places ──────────────────────────────────────────────────────────────

def _google_places_photo(name: str, place: str, api_key: str) -> str:
    try:
        r = requests.get(
            _PLACES_SEARCH,
            params={
                "input":     f"{name} {place}".strip(),
                "inputtype": "textquery",
                "fields":    "photos",
                "language":  "ja",
                "key":       api_key,
            },
            timeout=_TIMEOUT,
        )
        if not r.ok:
            return ""
        candidates = r.json().get("candidates", [])
        if not candidates:
            return ""
        photos = candidates[0].get("photos", [])
        if not photos:
            return ""
        ref = photos[0].get("photo_reference", "")
        if not ref:
            return ""
        return f"{_PLACES_PHOTO}?maxwidth=800&photo_reference={ref}&key={api_key}"
    except Exception:
        return ""


# ── Wikipedia REST API ────────────────────────────────────────────────────────

def _wikipedia_photo(name: str) -> str:
    clean = re.sub(r'[（(（][^）)）]*[）)）]', '', name).strip()
    for n in list(dict.fromkeys([clean, name])):
        url = _wiki_fetch(n)
        if url:
            return url
    return ""


def _wiki_fetch(name: str) -> str:
    try:
        encoded = quote(name, safe="")
        r = requests.get(
            _WIKI_SUMMARY.format(encoded),
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA},
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        if data.get("type") == "disambiguation":
            return ""

        # Prefer landscape original image
        orig = data.get("originalimage", {})
        ow, oh = orig.get("width", 0), orig.get("height", 1)
        if orig.get("source") and ow >= oh and ow >= 600:
            return orig["source"]

        # Fall back to thumbnail (upscale to 800px)
        thumb = data.get("thumbnail", {}).get("source", "")
        tw = data.get("thumbnail", {}).get("width", 0)
        th = data.get("thumbnail", {}).get("height", 1)
        if thumb and tw >= th:
            return re.sub(r"/\d+px-", "/800px-", thumb)

        return ""
    except Exception:
        return ""


# ── Wikimedia Commons search ──────────────────────────────────────────────────

def _commons_search(name: str) -> str:
    """Search Wikimedia Commons for landscape photos of a facility."""
    clean = re.sub(r'[（(（][^）)）]*[）)）]', '', name).strip()
    try:
        r = requests.get(
            _COMMONS_API,
            params={
                "action":      "query",
                "generator":   "search",
                "gsrsearch":   f"{clean} Japan",
                "gsrnamespace": "6",
                "prop":        "imageinfo",
                "iiprop":      "url|size",
                "iiurlwidth":  800,
                "format":      "json",
                "gsrlimit":    12,
            },
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA},
        )
        if not r.ok:
            return ""
        pages = r.json().get("query", {}).get("pages", {})
        best: tuple[float, str] = (0.0, "")
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            w = info.get("width", 0)
            h = max(info.get("height", 1), 1)
            url = info.get("thumburl", "")
            # Only landscape images with reasonable resolution
            if url and w >= h * 1.2 and w >= 600:
                ratio = w / h
                if ratio > best[0]:
                    best = (ratio, url)
        return best[1]
    except Exception:
        return ""


# ── Unsplash ──────────────────────────────────────────────────────────────────

def _unsplash_search(api_key: str, query: str) -> str:
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={
                "query":          query,
                "per_page":       1,
                "orientation":    "landscape",
                "content_filter": "high",
            },
            headers={"Authorization": f"Client-ID {api_key}"},
            timeout=_TIMEOUT,
        )
        if r.ok:
            results = r.json().get("results", [])
            if results:
                return results[0]["urls"]["regular"] + "&w=800&q=80"
    except Exception:
        pass
    return ""


# ── Category fallback ─────────────────────────────────────────────────────────

def _category_fallback(tags: list[str], spot_type: str) -> str:
    all_labels = (tags or []) + [spot_type or ""]
    for keywords, photo_id in _CATEGORY_FALLBACK:
        if any(label in keywords for label in all_labels):
            return f"{_UNSPLASH_BASE}{photo_id}{_PARAMS}"
    return f"{_UNSPLASH_BASE}{_DEFAULT_FALLBACK}{_PARAMS}"

from __future__ import annotations

import logging
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

import requests

_log = logging.getLogger(__name__)

# ── 30分キャッシュ ─────────────────────────────────────────────────────────────
_CACHE_TTL   = 1800                              # 30 minutes
_cache_store: dict[str, tuple[float, dict]] = {} # key → (stored_at, data)
_cache_lock  = Lock()


def _cache_key(*parts: str) -> str:
    return "|".join(parts)


def _cache_get(key: str) -> dict | None:
    with _cache_lock:
        entry = _cache_store.get(key)
        if entry and (_time.monotonic() - entry[0]) < _CACHE_TTL:
            return entry[1]
        _cache_store.pop(key, None)
        return None


def _cache_set(key: str, data: dict) -> None:
    if data.get("source") == "fallback":
        return  # フォールバックデータはキャッシュしない
    with _cache_lock:
        # キャッシュサイズ上限（50エントリ）
        if len(_cache_store) >= 50:
            oldest = min(_cache_store, key=lambda k: _cache_store[k][0])
            _cache_store.pop(oldest, None)
        _cache_store[key] = (_time.monotonic(), data)


try:
    TOKYO_TZ = ZoneInfo("Asia/Tokyo")
except Exception:
    TOKYO_TZ = timezone(timedelta(hours=9), "JST")

GEOCODING_URL    = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL     = "https://api.open-meteo.com/v1/forecast"
NOMINATIM_URL    = "https://nominatim.openstreetmap.org/reverse"
OWM_CURRENT_URL  = "https://api.openweathermap.org/data/2.5/weather"
OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


WEATHER_CODES = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々くもり", 3: "くもり",
    45: "霧", 48: "霧",
    51: "小雨", 53: "小雨", 55: "雨", 56: "雨", 57: "雨",
    61: "小雨", 63: "雨", 65: "強い雨", 66: "雨", 67: "雨",
    71: "雪", 73: "雪", 75: "大雪", 77: "雪",
    80: "にわか雨", 81: "雨", 82: "強い雨", 85: "雪", 86: "大雪",
    95: "雷雨", 96: "雷雨", 99: "雷雨",
}

# ── 天気アイコン ───────────────────────────────────────────────────────────────
_WEATHER_ICONS: dict[str, str] = {
    "快晴":         "☀️",
    "晴れ":         "🌤",
    "晴れ時々くもり": "⛅",
    "くもり":       "☁️",
    "霧":          "🌫",
    "小雨":         "🌦",
    "にわか雨":     "🌦",
    "雨":          "🌧",
    "強い雨":       "⛈",
    "雷雨":         "⛈",
    "雪":          "❄️",
    "大雪":         "☃️",
    "天気取得待ち":  "🌀",
}


def _condition_icon(condition: str) -> str:
    for key, icon in _WEATHER_ICONS.items():
        if key in condition:
            return icon
    if "雨" in condition or "雷" in condition:
        return "🌧"
    if "雪" in condition:
        return "❄️"
    if "晴" in condition:
        return "☀️"
    if "くもり" in condition or "霧" in condition:
        return "☁️"
    return "🌤"


# ── 都道府県 → 英語都市名 (OpenWeatherMap 用) ──────────────────────────────────
_PREF_TO_EN: dict[str, str] = {
    "北海道": "Sapporo",  "青森県": "Aomori",    "岩手県": "Morioka",
    "宮城県": "Sendai",   "秋田県": "Akita",     "山形県": "Yamagata",
    "福島県": "Fukushima","茨城県": "Mito",      "栃木県": "Utsunomiya",
    "群馬県": "Maebashi", "埼玉県": "Saitama",   "千葉県": "Chiba",
    "東京都": "Tokyo",    "神奈川県": "Yokohama", "新潟県": "Niigata",
    "富山県": "Toyama",   "石川県": "Kanazawa",  "福井県": "Fukui",
    "山梨県": "Kofu",     "長野県": "Nagano",    "岐阜県": "Gifu",
    "静岡県": "Shizuoka", "愛知県": "Nagoya",    "三重県": "Tsu",
    "滋賀県": "Otsu",     "京都府": "Kyoto",     "大阪府": "Osaka",
    "兵庫県": "Kobe",     "奈良県": "Nara",      "和歌山県": "Wakayama",
    "鳥取県": "Tottori",  "島根県": "Matsue",    "岡山県": "Okayama",
    "広島県": "Hiroshima","山口県": "Yamaguchi", "徳島県": "Tokushima",
    "香川県": "Takamatsu","愛媛県": "Matsuyama", "高知県": "Kochi",
    "福岡県": "Fukuoka",  "佐賀県": "Saga",      "長崎県": "Nagasaki",
    "熊本県": "Kumamoto", "大分県": "Oita",      "宮崎県": "Miyazaki",
    "鹿児島県": "Kagoshima","沖縄県": "Naha",
}


# ── wttr.in 天気コード → WMO コード変換 ──────────────────────────────────────
_WTTR_CODE_MAP: dict[int, int] = {
    113: 0,  116: 2,  119: 3,  122: 3,
    143: 45, 248: 45, 260: 45,
    176: 80, 179: 71, 185: 56, 200: 95,
    263: 51, 266: 51, 281: 56, 284: 57,
    293: 61, 296: 61, 299: 63, 302: 63,
    305: 65, 308: 65, 311: 66, 314: 67,
    317: 73, 320: 73, 323: 71, 326: 71,
    329: 73, 332: 73, 335: 75, 338: 75,
    350: 77, 353: 80, 356: 81, 359: 82,
    362: 85, 365: 85, 368: 85, 371: 86,
    386: 95, 389: 99, 392: 95, 395: 99,
}


def _wttr_to_wmo(code: int) -> int:
    return _WTTR_CODE_MAP.get(code, 3)


def _get_wttr_weather(
    query: str,
    start_time: str   = "10:00",
    end_time: str     = "17:00",
    location_name: str = "",
    latitude: float   = 0.0,
    longitude: float  = 0.0,
) -> dict[str, Any] | None:
    """wttr.in から天気を取得する (Open-Meteo forecast 代替)"""
    import urllib.parse
    url = f"https://wttr.in/{urllib.parse.quote(query)}?format=j1"
    try:
        resp = requests.get(url, timeout=10,
                            headers={"User-Agent": "outing-recommend-app/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log.warning("wttr.in failed for %r: %s", query, exc)
        return None

    try:
        current     = data.get("current_condition", [{}])[0]
        today_wx    = data.get("weather", [{}])[0]

        temp        = int(current.get("temp_C", "20"))
        wttr_code   = int(current.get("weatherCode", "113"))
        wmo_code    = _wttr_to_wmo(wttr_code)
        condition   = WEATHER_CODES.get(wmo_code, "天気情報あり")

        try:
            sh, sm = map(int, start_time.split(":"))
            eh, em = map(int, end_time.split(":"))
        except Exception:
            sh, sm, eh, em = 10, 0, 17, 0
        start_min = sh * 60 + sm
        end_min   = eh * 60 + em

        # 3時間刻み hourly: "time" = "0","300","600",...,"2100" (HH * 100)
        slots: list[dict] = []
        pops:  list[int]  = []
        for entry in today_wx.get("hourly", []):
            h         = int(entry["time"]) // 100
            e_temp    = float(entry.get("tempC", temp))
            e_rain    = int(entry.get("chanceofrain", 0))
            e_wmo     = _wttr_to_wmo(int(entry.get("weatherCode", wttr_code)))
            e_cond    = WEATHER_CODES.get(e_wmo, "天気情報あり")
            slot = {
                "time":         f"{h:02d}:00",
                "temperature":  e_temp,
                "rain_chance":  e_rain,
                "weather_code": e_wmo,
                "condition":    e_cond,
                "weather_icon": _condition_icon(e_cond),
                "wind_speed":   round(float(entry.get("windspeedKmph", 0)) / 3.6, 1),
            }
            slots.append(slot)
            if start_min <= h * 60 <= end_min:
                pops.append(e_rain)

        rain_chance = max(pops) if pops else int(current.get("chanceofrain", 0))
        rec         = decide_recommendation(rain_chance, wmo_code)
        icon        = _condition_icon(condition)
        loc         = location_name or query

        return {
            "condition":      condition,
            "temperature":    str(temp),
            "rain_chance":    str(rain_chance),
            "summary": (
                f"{loc}は{condition}、{temp}℃の予報です。"
                f"降水確率{rain_chance}%。{rec}のスポットがおすすめです。"
            ),
            "recommendation": rec,
            "location_name":  loc,
            "latitude":       latitude  or 35.681236,
            "longitude":      longitude or 139.767125,
            "hourly":         slots,
            "source":         "wttr.in",
            "weather_icon":   icon,
        }
    except Exception as exc:
        _log.warning("wttr.in parse error for %r: %s", query, exc)
        return None


def _owm_condition_ja(description: str) -> str:
    d = description.lower()
    if "thunderstorm" in d or "雷" in description:
        return "雷雨"
    if "drizzle" in d or "霧雨" in description:
        return "小雨"
    if "heavy rain" in d or "heavy intensity rain" in d:
        return "強い雨"
    if "rain" in d or "shower" in d or "雨" in description:
        return "雨"
    if "snow" in d or "雪" in description:
        return "雪"
    if "mist" in d or "fog" in d or "霧" in description:
        return "霧"
    if "overcast" in d:
        return "くもり"
    if "broken clouds" in d or "scattered clouds" in d:
        return "晴れ時々くもり"
    if "few clouds" in d:
        return "晴れ"
    if "clear" in d or "sunny" in d:
        return "快晴"
    return description or "天気情報あり"


def _owm_to_wmo_code(owm_id: int) -> int:
    if 200 <= owm_id < 300:
        return 95
    if 300 <= owm_id < 400:
        return 53
    if 500 <= owm_id < 600:
        return 65 if owm_id >= 502 else 61
    if 600 <= owm_id < 700:
        return 71
    if 700 <= owm_id < 800:
        return 45
    if owm_id == 800:
        return 0
    if owm_id == 801:
        return 1
    if owm_id == 802:
        return 2
    return 3


# ── OpenWeatherMap 取得 ────────────────────────────────────────────────────────

def _get_owm_weather(
    place: str      = "",
    start_time: str = "10:00",
    end_time: str   = "17:00",
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any] | None:
    """
    OpenWeatherMap API で天気を取得する。
    lat/lon が指定された場合は座標ベース、そうでなければ都市名ベースで検索。
    OPENWEATHER_API_KEY が未設定の場合は None を返す。
    """
    key = os.getenv("OPENWEATHER_API_KEY", "").strip()
    if not key:
        return None

    use_coords = lat is not None and lon is not None
    if use_coords:
        curr_params = {"lat": lat, "lon": lon, "appid": key, "lang": "ja", "units": "metric"}
        fc_params   = {"lat": lat, "lon": lon, "appid": key, "units": "metric", "cnt": 16}
        loc_label   = f"{lat:.4f},{lon:.4f}"
    else:
        city_en = _PREF_TO_EN.get(place, place)
        curr_params = {"q": f"{city_en},JP", "appid": key, "lang": "ja", "units": "metric"}
        fc_params   = {"q": f"{city_en},JP", "appid": key, "units": "metric", "cnt": 16}
        loc_label   = place or city_en

    try:
        # ── 現在の天気（気温・状態） ───────────────────────────────────────────
        curr_resp = requests.get(OWM_CURRENT_URL, params=curr_params, timeout=5)
        if not curr_resp.ok:
            _log.warning("OWM current HTTP %d for %r — %s",
                         curr_resp.status_code, loc_label, curr_resp.text[:400])
            return None
        curr = curr_resp.json()
        if curr.get("cod") != 200:
            _log.warning("OWM current cod=%s for %r — %s",
                         curr.get("cod"), loc_label, curr.get("message", ""))
            return None

        res_lat = float(curr["coord"]["lat"])
        res_lon = float(curr["coord"]["lon"])
        temp    = round(curr["main"]["temp"])
        owm_id  = curr["weather"][0]["id"]
        desc    = curr["weather"][0].get("description", "")
        condition = _owm_condition_ja(desc)

        # ── 予報（降水確率・時間別データ） ────────────────────────────────────
        try:
            sh, sm = map(int, start_time.split(":"))
            eh, em = map(int, end_time.split(":"))
            start_t = time(sh, sm)
            end_t   = time(eh, em)
        except Exception:
            start_t, end_t = time(10, 0), time(17, 0)

        fc_resp = requests.get(OWM_FORECAST_URL, params=fc_params, timeout=5)
        if not fc_resp.ok:
            _log.warning("OWM forecast HTTP %d for %r — %s",
                         fc_resp.status_code, loc_label, fc_resp.text[:400])
            # 予報取得失敗でも現在天気だけで返す
            wmo_code = _owm_to_wmo_code(owm_id)
            rec      = decide_recommendation(0, wmo_code)
            icon     = _condition_icon(condition)
            return {
                "condition": condition, "temperature": str(temp), "rain_chance": "0",
                "summary": f"{loc_label}は{condition}、{temp}℃です。",
                "recommendation": rec, "location_name": place or loc_label,
                "latitude": res_lat, "longitude": res_lon,
                "hourly": [], "source": "OpenWeatherMap", "weather_icon": icon,
            }

        fc_data = fc_resp.json()
        if fc_data.get("cod") != "200":
            _log.warning("OWM forecast cod=%s for %r — %s",
                         fc_data.get("cod"), loc_label, fc_data.get("message", ""))

        today  = datetime.now(TOKYO_TZ).date()
        hourly: list[dict] = []
        pops:   list[int]  = []

        for item in fc_data.get("list", []):
            dt_local  = datetime.fromtimestamp(item["dt"], tz=TOKYO_TZ)
            if dt_local.date() != today:
                continue
            item_t    = dt_local.time()
            item_pop  = int(item.get("pop", 0) * 100)
            item_id   = item["weather"][0]["id"]
            item_code = _owm_to_wmo_code(item_id)
            item_cond = _owm_condition_ja(item["weather"][0].get("description", ""))

            if start_t <= item_t <= end_t:
                pops.append(item_pop)

            hourly.append({
                "time":         dt_local.strftime("%H:%M"),
                "temperature":  float(item["main"]["temp"]),
                "rain_chance":  item_pop,
                "weather_code": item_code,
                "condition":    item_cond,
                "weather_icon": _condition_icon(item_cond),
                "wind_speed":   float(item.get("wind", {}).get("speed", 0)),
            })

        rain_chance = max(pops) if pops else 0
        wmo_code    = _owm_to_wmo_code(owm_id)
        rec         = decide_recommendation(rain_chance, wmo_code)
        icon        = _condition_icon(condition)

        return {
            "condition":      condition,
            "temperature":    str(temp),
            "rain_chance":    str(rain_chance),
            "summary": (
                f"{place or loc_label}は{condition}、{temp}℃の予報です。"
                f"降水確率{rain_chance}%。{rec}のスポットがおすすめです。"
            ),
            "recommendation": rec,
            "location_name":  place or loc_label,
            "latitude":       res_lat,
            "longitude":      res_lon,
            "hourly":         hourly,
            "source":         "OpenWeatherMap",
            "weather_icon":   icon,
        }

    except requests.exceptions.ConnectionError as exc:
        _log.warning("OWM connection error for %r: %s", loc_label, exc)
        return None
    except requests.exceptions.Timeout as exc:
        _log.warning("OWM timeout for %r: %s", loc_label, exc)
        return None
    except Exception as exc:
        _log.warning("OWM unexpected error for %r: %s", loc_label, exc)
        return None


# ── WeatherResult ─────────────────────────────────────────────────────────────

@dataclass
class WeatherResult:
    condition: str
    temperature: str
    rain_chance: str
    summary: str
    recommendation: str
    location_name: str
    latitude: float
    longitude: float
    hourly: list[dict[str, Any]]
    source: str       = "Open-Meteo"
    weather_icon: str = "🌤"

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition":      self.condition,
            "temperature":    self.temperature,
            "rain_chance":    self.rain_chance,
            "summary":        self.summary,
            "recommendation": self.recommendation,
            "location_name":  self.location_name,
            "latitude":       self.latitude,
            "longitude":      self.longitude,
            "hourly":         self.hourly,
            "source":         self.source,
            "weather_icon":   self.weather_icon,
        }


# ── Public API ────────────────────────────────────────────────────────────────

def reverse_geocode_place(latitude: float, longitude: float) -> str:
    try:
        params  = {"lat": latitude, "lon": longitude, "format": "json",
                   "accept-language": "ja", "zoom": 10}
        headers = {"User-Agent": "outing-recommend-app/1.0"}
        resp    = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        address = resp.json().get("address", {})
        return (
            address.get("state") or address.get("city")
            or address.get("town") or "現在地周辺"
        )
    except Exception:
        return "現在地周辺"


def get_weather_for_plan(place: str, time_label: str) -> dict[str, Any]:
    try:
        location = geocode_place(place)
        forecast = fetch_hourly_forecast(location["latitude"], location["longitude"])
        weather  = summarize_forecast(forecast, location, time_label)
        return weather.to_dict()
    except Exception:
        return fallback_weather(place, time_label)


def get_weather_for_plan_times(place: str, start_time: str, end_time: str) -> dict[str, Any]:
    """
    OWM → Open-Meteo → wttr.in → fallback の順で天気取得。
    同一条件は30分キャッシュ。
    """
    ck = _cache_key("place", place, start_time, end_time)
    if (hit := _cache_get(ck)) is not None:
        _log.info("Weather cache hit for %s %s-%s", place, start_time, end_time)
        return hit

    result = _fetch_weather_for_plan_times(place, start_time, end_time)
    _cache_set(ck, result)
    return result


def _fetch_weather_for_plan_times(
    place: str, start_time: str, end_time: str
) -> dict[str, Any]:
    # ── 1. OpenWeatherMap（APIキーが設定されている場合のみ） ──────────────────
    owm = _get_owm_weather(place, start_time, end_time)
    if owm:
        _log.info("Weather source: OpenWeatherMap for %s", place)
        return owm

    # ── 2. Open-Meteo（無料・キー不要） ──────────────────────────────────────
    location: dict[str, Any] | None = None
    try:
        location = geocode_place(place)
        forecast = fetch_hourly_forecast(location["latitude"], location["longitude"])
        slots    = select_time_slots_by_range(forecast.get("hourly", {}), start_time, end_time)
        if not slots:
            raise ValueError("対象時間帯のスロットなし")

        avg_temp      = round(sum(s["temperature"] for s in slots) / len(slots))
        max_rain      = max(s["rain_chance"] for s in slots)
        dominant_code = max(
            {s["weather_code"] for s in slots},
            key=lambda code: sum(1 for s in slots if s["weather_code"] == code),
        )
        condition      = WEATHER_CODES.get(dominant_code, "天気情報あり")
        recommendation = decide_recommendation(max_rain, dominant_code)
        icon           = _condition_icon(condition)
        _log.info("Weather source: Open-Meteo for %s", place)

        return WeatherResult(
            condition=condition,
            temperature=str(avg_temp),
            rain_chance=str(max_rain),
            summary=(
                f"{start_time}〜{end_time}は{condition}の見込みです。"
                f"最高降水確率{max_rain}%。{recommendation}のスポットがおすすめです。"
            ),
            recommendation=recommendation,
            location_name=location["name"],
            latitude=location["latitude"],
            longitude=location["longitude"],
            hourly=slots,
            weather_icon=icon,
        ).to_dict()
    except Exception as exc:
        _log.warning("Open-Meteo forecast failed for %s: %s", place, exc)

    # ── 3. wttr.in フォールバック ─────────────────────────────────────────────
    city_en  = _PREF_TO_EN.get(place, place)
    lat      = location["latitude"]  if location else 35.681236
    lon      = location["longitude"] if location else 139.767125
    loc_name = location["name"]      if location else place
    wttr = _get_wttr_weather(city_en, start_time, end_time,
                              location_name=loc_name, latitude=lat, longitude=lon)
    if wttr:
        _log.info("Weather source: wttr.in for %s", place)
        return wttr

    return fallback_weather(place, f"{start_time}〜{end_time}")


def get_weather_for_coordinates(
    latitude: float,
    longitude: float,
    time_label: str = "昼から夕方",
) -> dict[str, Any]:
    """座標ベース天気取得。同一座標（1/100度単位）・同一時間帯は30分キャッシュ。"""
    # time_label が "HH:MM〜HH:MM" 形式ならそのまま利用
    if "〜" in time_label:
        parts   = time_label.split("〜")
        start_t = parts[0].strip()
        end_t   = parts[1].strip()
    else:
        start_t, end_t = "10:00", "20:00"

    # 座標を 0.01 度単位 (~1km) に丸めてキャッシュキーとして使用
    ck = _cache_key("coords",
                    f"{round(latitude, 2)}", f"{round(longitude, 2)}",
                    start_t, end_t)
    if (hit := _cache_get(ck)) is not None:
        _log.info("Weather cache hit for coords %.2f,%.2f", latitude, longitude)
        return hit

    result = _fetch_weather_for_coordinates(latitude, longitude, time_label, start_t, end_t)
    _cache_set(ck, result)
    return result


def _fetch_weather_for_coordinates(
    latitude: float, longitude: float,
    time_label: str, start_t: str, end_t: str,
) -> dict[str, Any]:
    # ── 1. OpenWeatherMap（APIキーが設定されている場合のみ） ──────────────────
    owm = _get_owm_weather(lat=latitude, lon=longitude,
                           start_time=start_t, end_time=end_t)
    if owm:
        _log.info("Weather source: OpenWeatherMap (coords)")
        return owm

    # ── 2. Open-Meteo ─────────────────────────────────────────────────────────
    try:
        location = {"name": "現在地周辺", "latitude": latitude, "longitude": longitude}
        forecast = fetch_hourly_forecast(latitude, longitude)
        weather  = summarize_forecast(forecast, location, time_label)
        _log.info("Weather source: Open-Meteo (coords)")
        return weather.to_dict()
    except Exception as exc:
        _log.warning("Open-Meteo coords failed (%.4f, %.4f): %s", latitude, longitude, exc)

    # ── 3. wttr.in フォールバック ─────────────────────────────────────────────
    wttr = _get_wttr_weather(
        f"{latitude:.4f},{longitude:.4f}",
        start_t, end_t,
        location_name="現在地周辺",
        latitude=latitude, longitude=longitude,
    )
    if wttr:
        _log.info("Weather source: wttr.in (coords)")
        return wttr

    return fallback_weather("現在地周辺", time_label)


# ── Internal helpers ──────────────────────────────────────────────────────────

def geocode_place(place: str) -> dict[str, Any]:
    for query in _place_candidates(place or "東京"):
        result = _try_geocode(query)
        if result:
            return result
    raise ValueError("場所が見つかりませんでした")


def _place_candidates(place: str) -> list[str]:
    candidates = [place]
    stripped   = place
    for suffix in ("駅", "区", "町", "村", "市街"):
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
            candidates.append(stripped)
            break
    admin_suffixes = ("都", "道", "府", "県", "市", "町", "村")
    if not any(stripped.endswith(s) for s in admin_suffixes):
        candidates += [stripped + "市", stripped + "都", stripped + "府"]
    return candidates


def _try_geocode(place: str) -> dict[str, Any] | None:
    params   = {"name": place, "count": 1, "language": "ja", "format": "json"}
    response = requests.get(GEOCODING_URL, params=params, timeout=5)
    response.raise_for_status()
    results  = response.json().get("results") or []
    if not results:
        return None
    result = results[0]
    return {
        "name":      result.get("name", place),
        "latitude":  float(result["latitude"]),
        "longitude": float(result["longitude"]),
    }


def fetch_hourly_forecast(latitude: float, longitude: float) -> dict[str, Any]:
    params = {
        "latitude":  latitude,
        "longitude": longitude,
        "hourly":    "temperature_2m,precipitation_probability,weather_code,windspeed_10m",
        "timezone":  "Asia/Tokyo",
        "forecast_days": 2,
    }
    response = requests.get(FORECAST_URL, params=params, timeout=(3, 3))
    response.raise_for_status()
    return response.json()


def summarize_forecast(
    forecast: dict[str, Any],
    location: dict[str, Any],
    time_label: str,
) -> WeatherResult:
    slots = select_time_slots(forecast.get("hourly", {}), time_label)
    if not slots:
        raise ValueError("対象時間帯の天気データがありません")

    avg_temp      = round(sum(slot["temperature"] for slot in slots) / len(slots))
    max_rain      = max(slot["rain_chance"] for slot in slots)
    dominant_code = max(
        {slot["weather_code"] for slot in slots},
        key=lambda code: sum(1 for slot in slots if slot["weather_code"] == code),
    )
    condition     = WEATHER_CODES.get(dominant_code, "天気情報あり")
    recommendation = decide_recommendation(max_rain, dominant_code)
    icon          = _condition_icon(condition)
    summary       = (
        f"{time_label}は{condition}の見込みです。"
        f"最高降水確率は{max_rain}%なので、{recommendation}の予定がおすすめです。"
    )
    return WeatherResult(
        condition=condition,
        temperature=str(avg_temp),
        rain_chance=str(max_rain),
        summary=summary,
        recommendation=recommendation,
        location_name=location["name"],
        latitude=location["latitude"],
        longitude=location["longitude"],
        hourly=slots,
        weather_icon=icon,
    )


def select_time_slots(hourly: dict[str, list[Any]], time_label: str) -> list[dict[str, Any]]:
    today      = datetime.now(TOKYO_TZ).date()
    start_hour, end_hour = time_range_for_label(time_label)
    wind_speeds = hourly.get("windspeed_10m", [])
    slots      = []
    for i, (raw_time, temperature, rain_chance, weather_code) in enumerate(zip(
        hourly.get("time", []),
        hourly.get("temperature_2m", []),
        hourly.get("precipitation_probability", []),
        hourly.get("weather_code", []),
    )):
        forecast_time = datetime.fromisoformat(raw_time)
        if forecast_time.date() != today:
            continue
        if start_hour <= forecast_time.time() <= end_hour:
            cond = WEATHER_CODES.get(int(weather_code or 0), "天気情報あり")
            slots.append({
                "time":         forecast_time.strftime("%H:%M"),
                "temperature":  float(temperature),
                "rain_chance":  int(rain_chance or 0),
                "weather_code": int(weather_code or 0),
                "condition":    cond,
                "weather_icon": _condition_icon(cond),
                "wind_speed":   round(float(wind_speeds[i]) / 3.6, 1) if i < len(wind_speeds) else 0.0,
            })
    return slots


def select_time_slots_by_range(
    hourly: dict[str, list[Any]],
    start_time: str,
    end_time: str,
) -> list[dict[str, Any]]:
    today = datetime.now(TOKYO_TZ).date()
    try:
        sh, sm  = map(int, start_time.split(":"))
        eh, em  = map(int, end_time.split(":"))
        start_t = time(sh, sm)
        end_t   = time(eh, em)
    except Exception:
        start_t, end_t = time(10, 0), time(17, 0)

    wind_speeds = hourly.get("windspeed_10m", [])
    slots = []
    for i, (raw_time, temperature, rain_chance, weather_code) in enumerate(zip(
        hourly.get("time", []),
        hourly.get("temperature_2m", []),
        hourly.get("precipitation_probability", []),
        hourly.get("weather_code", []),
    )):
        forecast_time = datetime.fromisoformat(raw_time)
        if forecast_time.date() != today:
            continue
        if start_t <= forecast_time.time() <= end_t:
            cond = WEATHER_CODES.get(int(weather_code or 0), "天気情報あり")
            slots.append({
                "time":         forecast_time.strftime("%H:%M"),
                "temperature":  float(temperature),
                "rain_chance":  int(rain_chance or 0),
                "weather_code": int(weather_code or 0),
                "condition":    cond,
                "weather_icon": _condition_icon(cond),
                "wind_speed":   round(float(wind_speeds[i]) / 3.6, 1) if i < len(wind_speeds) else 0.0,
            })
    return slots


def time_range_for_label(time_label: str) -> tuple[time, time]:
    # "HH:MM〜HH:MM" 形式を直接パース
    if "〜" in time_label:
        parts = time_label.split("〜")
        try:
            sh, sm = map(int, parts[0].strip().split(":"))
            eh, em = map(int, parts[1].strip().split(":"))
            return time(sh, sm), time(eh, em)
        except Exception:
            pass
    if "午前" in time_label:
        return time(9, 0), time(12, 0)
    if "夕方" in time_label and "夜" in time_label:
        return time(17, 0), time(21, 0)
    if "終日" in time_label:
        return time(10, 0), time(20, 0)
    return time(12, 0), time(18, 0)


def decide_recommendation(max_rain: int, weather_code: int) -> str:
    rainy_codes = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
    if max_rain >= 50 or weather_code in rainy_codes:
        return "屋内中心"
    if max_rain >= 30:
        return "屋内と屋外の両方"
    return "屋外寄り"


def fallback_weather(place: str, time_label: str) -> dict[str, Any]:
    return WeatherResult(
        condition="天気取得待ち",
        temperature="--",
        rain_chance="--",
        summary=f"{place or '指定地点'}の天気を取得できなかったため、仮の条件で候補を表示します。",
        recommendation="屋内と屋外の両方",
        location_name=place or "指定地点",
        latitude=35.681236,
        longitude=139.767125,
        hourly=[],
        source="fallback",
        weather_icon="🌀",
    ).to_dict()


# ── 起動時プロバイダーログ（load_dotenv() 後にインポートされることを前提） ────────
def _announce_weather_provider() -> None:
    if os.getenv("OPENWEATHER_API_KEY", "").strip():
        _log.info(
            "Weather provider: OpenWeatherMap "
            "(fallback: Open-Meteo → wttr.in → static)"
        )
    else:
        _log.info(
            "Weather provider: Open-Meteo "
            "(fallback: wttr.in → static)"
        )

_announce_weather_provider()

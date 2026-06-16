import datetime
import logging
import os
import random
import uuid

from dotenv import load_dotenv

# .env を最初にロードしてからサービスをインポート（起動時ログが env を参照するため）
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for

from models.favorite import add_favorite, favorite_ids, load_favorites, remove_favorite
from services.gemini_service import fallback_recommendations, generate_recommendations
from services.history_service import add_spots as history_add_spots, get_penalty_map
from services.places_service import check_places_api
from services.weather_service import (
    get_weather_for_coordinates,
    get_weather_for_plan,
    get_weather_for_plan_times,
    reverse_geocode_place,
)
from utils.helpers import DEFAULT_SEARCH, build_search_from_form, calc_duration, time_label, validate_search
from utils.plan_store import load as store_load, save as store_save

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.template_filter("yen")
def yen_filter(value: object) -> str:
    try:
        return f"¥{int(value):,}"
    except (ValueError, TypeError):
        return f"¥{value}"

# ── 起動時 Places API 接続テスト ──────────────────────────────────────────────
_places_api_ok, _places_api_warn = check_places_api()


@app.context_processor
def _inject_api_status() -> dict:
    return {"places_api_warning": _places_api_warn}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _crowd_prediction(types: list, start_time: str, weekday: int) -> str:
    """スポットタイプ・時間・曜日から混雑度を予測。low / medium / high を返す。"""
    try:
        h = int(start_time.split(":")[0])
    except Exception:
        h = 12
    t = set(types or [])

    if t & {"amusement_park", "zoo"}:
        base = 2
    elif t & {"tourist_attraction", "aquarium", "museum", "shopping_mall"}:
        base = 1
    else:
        base = 0

    if weekday >= 5:          # 土日
        base = min(2, base + 1)
    if 11 <= h <= 14 or 15 <= h <= 17:  # ピーク帯
        base = min(2, base + 1)
    elif h < 9 or h >= 19:   # 早朝・夜
        base = max(0, base - 1)

    return ["low", "medium", "high"][base]


def _weather_type(condition: str) -> str:
    if "雨" in condition or "雷" in condition:
        return "rain"
    if "くもり" in condition or "霧" in condition:
        return "cloud"
    return "sunny"


def _find_plan(plan_id: str) -> dict | None:
    for plan in store_load("plans", []):
        if plan.get("id") == plan_id:
            return plan
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    weather = store_load("weather") or {
        "condition": "晴れ", "temperature": 20, "rain_chance": 0,
        "summary": "", "recommendation": "屋外", "location_name": "東京"
    }
    return render_template("index.html", weather=weather)


@app.route("/today")
def today_page():
    plans   = store_load("plans")
    weather = store_load("weather") or {}
    search  = store_load("search")  or DEFAULT_SEARCH

    if not plans:
        plans = fallback_recommendations(search, weather)
        store_save("plans", plans)

    try:
        n = int(request.args.get("n", 0))
    except (ValueError, TypeError):
        n = 0
    n = n % max(len(plans), 1)

    spot   = plans[n]
    wtype  = _weather_type(weather.get("condition", ""))
    saved  = favorite_ids()
    return render_template(
        "today.html",
        spot=spot,
        weather=weather,
        saved_ids=saved,
        wtype=wtype,
        current_n=n,
        total=len(plans),
    )


@app.get("/api/location")
def current_location():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"ok": False, "message": "位置情報の値が正しくありません。"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"ok": False, "message": "位置情報の値が正しくありません。"}), 400
    return jsonify({"ok": True, "place": reverse_geocode_place(lat, lon)})


@app.get("/api/weather/current")
def current_weather():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"ok": False, "message": "現在地を取得できませんでした。"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"ok": False, "message": "位置情報の値が正しくありません。"}), 400
    return jsonify({"ok": True, "weather": get_weather_for_coordinates(lat, lon)})


@app.route("/input", methods=["GET", "POST"])
def input_page():
    if request.method == "POST":
        search = build_search_from_form(request.form)
        errors = validate_search(search)
        if errors:
            return render_template("input.html", search=search, errors=errors), 400

        # GPS座標があれば座標ベース天気取得（より正確）
        try:
            u_lat = float(search.get("user_lat") or "")
            u_lon = float(search.get("user_lon") or "")
            weather = get_weather_for_coordinates(
                u_lat, u_lon,
                f"{search.get('start_time','10:00')}〜{search.get('end_time','17:00')}",
            )
            weather.setdefault("location_name", search["place"])
        except (ValueError, TypeError):
            weather = get_weather_for_plan_times(
                search["place"],
                search.get("start_time", "10:00"),
                search.get("end_time",   "17:00"),
            )
        # セッションIDを確立（永続履歴の識別子）
        if "session_id" not in session:
            session["session_id"] = uuid.uuid4().hex
        session_id: str = session["session_id"]

        # セッション内履歴（旧: session-only）
        spot_history: list[str] = session.get("spot_history", [])
        search["spot_history"] = spot_history

        # 永続履歴からペナルティマップを取得してsearchに注入
        search["penalty_map"] = get_penalty_map(session_id)

        plans = generate_recommendations(search, weather)

        # 今回提案したスポット名を両方の履歴へ追加
        new_spots: list[str] = []
        for plan in plans:
            for spot in plan.get("spots", []):
                name = spot.get("name", "")
                if name and name not in spot_history:
                    new_spots.append(name)
        spot_history = (spot_history + new_spots)[-50:]
        session["spot_history"]  = spot_history
        session["has_results"]   = True

        # 永続履歴へ保存（次回以降のペナルティ計算に使用）
        if new_spots:
            history_add_spots(session_id, new_spots)

        store_save("plans",   plans)
        store_save("weather", weather)
        store_save("search",  search)
        flash("条件に合わせて候補を生成しました。")
        return redirect(url_for("result"))

    raw_search     = store_load("search")
    autofill_place = raw_search is None
    search         = dict(raw_search or DEFAULT_SEARCH)
    for key in ("people", "start_time", "end_time", "purpose", "budget"):
        val = request.args.get(key)
        if val:
            search[key] = val
    return render_template("input.html", search=search, errors={}, autofill_place=autofill_place)


@app.route("/result")
def result():
    plans   = store_load("plans")
    weather = store_load("weather")
    search  = store_load("search")

    if not plans or not weather or not search:
        flash("先に条件を入力してください。")
        return redirect(url_for("input_page"))

    wtype    = _weather_type(weather.get("condition", ""))
    saved    = favorite_ids()
    duration = calc_duration(
        search.get("start_time", "10:00"),
        search.get("end_time",   "17:00"),
    )
    tlabel = time_label(
        search.get("start_time", "10:00"),
        search.get("end_time",   "17:00"),
    )
    return render_template(
        "result.html",
        weather=weather,
        plans=plans,
        search=search,
        saved_ids=saved,
        wtype=wtype,
        duration=duration,
        time_label=tlabel,
    )


@app.route("/roulette")
def roulette():
    all_plans = store_load("plans")
    weather   = store_load("weather") or {}
    search    = store_load("search")  or DEFAULT_SEARCH
    if not all_plans:
        all_plans = fallback_recommendations(search, weather)
    plans = all_plans[:10]
    return render_template("roulette.html", plans=plans)


@app.route("/saved")
def saved():
    return render_template("saved.html", plans=load_favorites())


@app.post("/api/favorites/<plan_id>")
def save_favorite(plan_id):
    plan = _find_plan(plan_id)
    if not plan:
        return jsonify({"ok": False, "message": "候補が見つかりませんでした"}), 404
    favorite = add_favorite(plan)
    return jsonify({"ok": True, "saved": True, "favorite": favorite})


@app.delete("/api/favorites/<plan_id>")
def delete_favorite(plan_id):
    remove_favorite(plan_id)
    return jsonify({"ok": True, "saved": False})


@app.route("/plan/<plan_id>")
def plan_detail(plan_id):
    plan = _find_plan(plan_id)
    if not plan:
        # favoriteからも探す
        plan = next((p for p in load_favorites() if p.get("id") == plan_id), None)
    if not plan:
        flash("プランが見つかりませんでした。再度条件を入力してください。")
        return redirect(url_for("input_page"))

    weather = store_load("weather") or {}
    search  = store_load("search")  or DEFAULT_SEARCH
    saved   = favorite_ids()

    # スポットへ混雑予測を付与
    weekday = datetime.date.today().weekday()
    for spot in plan.get("spots", []):
        spot["crowd_level"] = _crowd_prediction(
            spot.get("types", []), spot.get("start_time", "12:00"), weekday,
        )

    return render_template(
        "plan_detail.html",
        plan=plan,
        weather=weather,
        search=search,
        saved_ids=saved,
        today=datetime.date.today().strftime("%Y年%m月%d日"),
        plan_id=plan_id,
    )


@app.get("/api/spot_details/<place_id>")
def spot_details_api(place_id):
    """Places Details APIで営業時間・電話・サイトを取得する。"""
    api_key = os.getenv("PLACES_API_KEY", "").strip()
    if not api_key or not place_id:
        return jsonify({"ok": False})
    try:
        import requests as _req
        resp = _req.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "place_id": place_id,
                "fields":   "opening_hours,formatted_phone_number,website",
                "language": "ja",
                "key":      api_key,
            },
            timeout=5,
        )
        result = resp.json().get("result", {})
        hours  = result.get("opening_hours", {})
        return jsonify({
            "ok":           True,
            "open_now":     hours.get("open_now"),
            "weekday_text": hours.get("weekday_text", []),
            "phone":        result.get("formatted_phone_number", ""),
            "website":      result.get("website", ""),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.post("/api/visited")
def mark_visited():
    """スポットを「行った」マークし、履歴ペナルティ対象にする。"""
    data      = request.get_json(force=True) or {}
    spot_name = str(data.get("spot_name", "")).strip()
    if spot_name and "session_id" in session:
        history_add_spots(session["session_id"], [spot_name])
    return jsonify({"ok": True})


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

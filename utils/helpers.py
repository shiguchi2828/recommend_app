from __future__ import annotations


DEFAULT_SEARCH = {
    "people":     "カップル",
    "start_time": "10:00",
    "end_time":   "17:00",
    "place":      "東京都",
    "purpose":    "自然",
    "budget":     "気にしない",
    "user_lat":   "",   # GPS取得した緯度（取得できた場合のみ）
    "user_lon":   "",   # GPS取得した経度
}


FIELD_OPTIONS = {
    "people":  {"ひとり", "カップル", "友達", "家族"},
    "purpose": {"自然", "グルメ", "写真映え", "温泉", "歴史", "アクティブ", "ゆっくり", "デート",
                "SNS映え", "食べ歩き"},   # レガシー互換
    "budget":  {"1,000円以内", "3,000円以内", "5,000円以内",
                "10,000円以内", "15,000円以内", "20,000円以内", "30,000円以内",
                "気にしない"},
}


def calc_duration(start: str, end: str) -> float:
    """Return duration in hours (float). Returns 0.0 on parse error."""
    try:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        minutes = (eh * 60 + em) - (sh * 60 + sm)
        return max(0.0, round(minutes / 60, 1))
    except Exception:
        return 0.0


def time_label(start: str, end: str) -> str:
    """Human-readable label, e.g. '10:00〜17:00（7時間）'."""
    dur = calc_duration(start, end)
    dur_str = f"{int(dur)}時間" if dur == int(dur) else f"{dur}時間"
    return f"{start}〜{end}（{dur_str}）"


def build_search_from_form(form) -> dict:
    search: dict = {}
    for field, default in DEFAULT_SEARCH.items():
        if field == "purpose":
            # Multi-select checkboxes → join with comma
            values = form.getlist("purpose") if hasattr(form, "getlist") else [form.get("purpose", "")]
            joined = ",".join(v.strip() for v in values if v and v.strip())
            search["purpose"] = joined or default
        else:
            value = form.get(field, default)
            search[field] = value.strip() if isinstance(value, str) else default

    # GPS座標: 数値変換のみ（バリデーションはplan_builderで行う）
    for coord_field in ("user_lat", "user_lon"):
        raw = form.get(coord_field, "").strip()
        try:
            search[coord_field] = str(float(raw)) if raw else ""
        except ValueError:
            search[coord_field] = ""

    return search


def validate_search(search: dict) -> dict:
    errors: dict = {}

    if not search.get("place"):
        errors["place"] = "エリアを入力してください。"

    start = search.get("start_time", "")
    end   = search.get("end_time", "")

    if not start:
        errors["start_time"] = "開始時間を入力してください。"
    if not end:
        errors["end_time"] = "終了時間を入力してください。"

    if start and end:
        if calc_duration(start, end) <= 0:
            errors["end_time"] = "終了時間は開始時間より後にしてください。"

    # purpose: validate each selected purpose individually
    valid_purposes = FIELD_OPTIONS["purpose"]
    raw_purpose    = search.get("purpose", "")
    purposes       = [p.strip() for p in raw_purpose.split(",") if p.strip()]
    if not purposes:
        errors["purpose"] = "気分を1つ以上選択してください。"
    elif not all(p in valid_purposes for p in purposes):
        errors["purpose"] = "選択肢から選んでください。"

    for field in ("people", "budget"):
        if search.get(field) not in FIELD_OPTIONS[field]:
            errors[field] = "選択肢から選んでください。"

    return errors

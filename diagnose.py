# -*- coding: utf-8 -*-
"""
Places API + Gemini diagnostic script
Usage: python diagnose.py
"""
import logging
import os
import sys

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("diagnose")

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

# ── 1. APIkey check ─────────────────────────────────────────────────────────────
google_key = os.getenv("GOOGLE_API_KEY", "").strip()
gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

print("\n" + "="*60)
print("[1] APIキー確認")
print("="*60)
print(f"  GOOGLE_API_KEY : {'SET (' + str(len(google_key)) + 'chars)' if google_key else 'NG: 未設定'}")
print(f"  GEMINI_API_KEY : {'SET (' + str(len(gemini_key)) + 'chars)' if gemini_key else 'NG: 未設定'}")

if not google_key:
    print("\nNG: GOOGLE_API_KEY が未設定です。.env に設定してください。")
    sys.exit(1)

# ── 2. Places API test ─────────────────────────────────────────────────────────
print("\n" + "="*60)
print("[2] Google Places API テスト (東京都, カップル, デート)")
print("="*60)

from services.places_service import fetch_spots, _build_queries

queries = _build_queries("東京都", "カップル", "デート", False)
print(f"\n生成クエリ ({len(queries)}件):")
for i, q in enumerate(queries, 1):
    print(f"  {i}. {q}")

print("\n取得中...")
try:
    spots = fetch_spots(place="東京都", people="カップル", purpose="デート")
    print(f"\nOK: Places API 取得成功: {len(spots)}件")
    print("\n取得スポット一覧:")
    for s in spots:
        has_photo = "photo:YES" if s.get("photo_url") else "photo:NO "
        addr      = s.get("address", "")[:35]
        print(f"  [{has_photo}] star={s.get('rating',0):.1f}({s.get('review_count',0):4d}rev)  {s['name']:<30} {addr}")
except Exception as e:
    print(f"\nNG: Places API エラー: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

if not spots:
    print("\nNG: スポットが0件。APIキーが有効か・Places APIが有効化されているか確認してください。")
    sys.exit(1)

# ── 3. Gemini + Places integration test ─────────────────────────────────────────
if not gemini_key:
    print("\nNOTE: GEMINI_API_KEY 未設定のため Gemini テストをスキップ")
    sys.exit(0)

print("\n" + "="*60)
print("[3] Gemini + Places 統合テスト")
print("="*60)

from services.gemini_service import generate_recommendations

search  = {"place": "東京都", "people": "カップル", "purpose": "デート",
           "start_time": "10:00", "end_time": "17:00", "budget": "気にしない"}
weather = {"condition": "晴れ", "temperature": 22, "rain_chance": 10,
           "recommendation": "屋外も快適な天気です", "hourly": []}

print("\nGemini 呼び出し中...(30〜60秒かかります)")
try:
    plans = generate_recommendations(search, weather)
    print(f"\nOK: 生成成功: {len(plans)}件")
    print("\n生成スポット一覧:")
    for p in plans:
        photo_src = "Places" if "maps.googleapis.com/maps/api/place/photo" in (p.get("photo_url") or "") else "fallback"
        n_slots   = len(p.get("schedule_items", []))
        print(f"  star={p.get('rating',0):.1f} [{photo_src}] slots={n_slots}  {p['name']}")
        for item in p.get("schedule_items", []):
            t1 = item.get("time", "")
            t2 = item.get("end_time", "")
            pl = item.get("place", "")
            ac = item.get("activity", "")
            mv = item.get("travel_min", 0)
            print(f"      {t1}〜{t2}  {pl}  {ac}  (移動{mv}分)")
except Exception as e:
    print(f"\nNG: Gemini エラー: {e}")
    import traceback; traceback.print_exc()

print("\n" + "="*60)
print("診断完了")
print("="*60)

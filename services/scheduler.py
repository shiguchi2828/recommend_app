"""
Python-controlled time slot scheduler.

Guarantees [start_time, end_time] is covered with NO gaps.
Key rule: every non-last spot gets exactly `base_slot` minutes
(type-ideal is used only as a lower-bound reference — never to shrink slots).
The last spot always ends at end_time precisely.
"""
from __future__ import annotations
from typing import Any

# Soft lower-bound stay duration by Google Places type (reference only)
_TYPE_MIN: dict[str, int] = {
    "restaurant": 60, "food": 60, "meal_delivery": 45, "meal_takeaway": 40,
    "cafe": 50, "bakery": 40, "bar": 70, "night_club": 80,
    "museum": 80, "art_gallery": 70, "movie_theater": 100,
    "amusement_park": 110, "zoo": 110, "aquarium": 90, "stadium": 80,
    "park": 60, "natural_feature": 55, "campground": 80,
    "shopping_mall": 80, "store": 45,
    "tourist_attraction": 65, "point_of_interest": 55,
    "spa": 80, "lodging": 0,
}
_DEFAULT_MIN = 60


def to_min(hhmm: str) -> int:
    try:
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m
    except Exception:
        return 0


def from_min(mins: int) -> str:
    mins = max(0, min(mins, 23 * 60 + 59))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _type_duration(types: list[str]) -> int:
    for t in (types or []):
        if t in _TYPE_MIN:
            return _TYPE_MIN[t]
    return _DEFAULT_MIN


def ideal_spot_count(start_time: str, end_time: str) -> int:
    """Return spot count in [4, 6] range based on available hours."""
    total = max(to_min(end_time) - to_min(start_time), 0)
    if total < 180:    # <3h
        return 3
    if total < 300:    # 3〜5h
        return 4
    if total < 420:    # 5〜7h
        return 5
    return 6           # 7h+


def schedule_spots(
    spots: list[dict[str, Any]],
    start_time: str,
    end_time: str,
    travel_min: int = 15,
    travel_times: list[int] | None = None,
) -> list[dict[str, Any]]:
    """
    Assign start_time / end_time to each spot.

    Guarantee: [start_time, end_time] is fully covered — NO gaps.

    - travel_times: optional per-hop travel list (length = n-1).
      When provided, replaces the fixed travel_min for each hop.
    - Stay durations are proportional to spot type preference.
    - Last spot always ends EXACTLY at end_time.
    """
    if not spots:
        return []

    start_min = to_min(start_time)
    end_min   = to_min(end_time)
    total_min = max(end_min - start_min, 60)
    n         = len(spots)

    # Build per-hop travel list (length n, last element = 0)
    if travel_times and len(travel_times) >= n - 1:
        hops: list[int] = list(travel_times[:n - 1]) + [0]
    else:
        hops = [travel_min] * (n - 1) + [0]

    travel_total = sum(hops[: n - 1])
    available    = max(total_min - travel_total, n * 30)

    # Proportional stay durations based on spot type preference
    preferred    = [_type_duration(spot.get("types", [])) for spot in spots]
    total_pref   = sum(preferred) or (n * _DEFAULT_MIN)
    scale        = available / total_pref
    durations    = [max(30, int(p * scale)) for p in preferred]

    result: list[dict[str, Any]] = []
    current = start_min

    for i, spot in enumerate(spots):
        is_last = (i == n - 1)

        if is_last:
            t_end  = end_min
            travel = 0
        else:
            t_end = current + durations[i]
            # Reserve: each remaining spot needs ≥30 min + its outbound travel
            remaining   = n - 1 - i
            min_reserve = remaining * 30 + sum(hops[i : n - 1])
            t_end = min(t_end, end_min - min_reserve)
            t_end = max(t_end, current + 30)
            travel = hops[i]

        out = dict(spot)
        out["start_time"] = from_min(current)
        out["end_time"]   = from_min(t_end)
        out["travel_min"] = travel
        result.append(out)
        current = t_end + travel

    # Guarantee exact string match for end_time (avoids from_min rounding edge cases)
    if result:
        result[-1]["end_time"]   = end_time
        result[-1]["travel_min"] = 0

    return result

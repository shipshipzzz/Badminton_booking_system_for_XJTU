"""Dual-slot pairing helpers. Pure functions, no I/O."""

from __future__ import annotations

from models import TIME_SLOT_LABELS


def next_slot_time(time_str: str) -> str | None:
    try:
        idx = TIME_SLOT_LABELS.index(time_str)
    except ValueError:
        return None
    if idx + 1 >= len(TIME_SLOT_LABELS):
        return None
    return TIME_SLOT_LABELS[idx + 1]


def _time_pref_map(profile: dict) -> dict[str, dict]:
    return {t.get("time"): t for t in (profile.get("time_prefs") or []) if t.get("time")}


def get_dual_slot_prefs(profile: dict) -> list[dict]:
    if not profile.get("dual_slot_enabled"):
        return []
    tp_map = _time_pref_map(profile)
    result = []
    for raw in profile.get("dual_slot_prefs") or []:
        start = raw.get("time")
        nxt = next_slot_time(start)
        if not start or not nxt:
            continue
        tp = tp_map.get(start) or {}
        try:
            priority = int(raw.get("priority") or 1)
        except (TypeError, ValueError):
            priority = 1
        try:
            fanout = int(tp.get("fanout") or 4)
        except (TypeError, ValueError):
            fanout = 4
        try:
            parallel = int(tp.get("parallel") or 1)
        except (TypeError, ValueError):
            parallel = 1
        result.append({
            "time": start,
            "next_time": nxt,
            "priority": max(1, min(priority, 5)),
            "fanout": max(0, min(fanout, 10)),
            "parallel": max(1, min(parallel, 10)),
        })
    return result


def dual_occupied_times(dual_prefs: list[dict]) -> set[str]:
    occupied: set[str] = set()
    for item in dual_prefs:
        occupied.add(item["time"])
        occupied.add(item["next_time"])
    return occupied


def filter_single_time_prefs(time_prefs: list[dict], occupied: set[str]) -> list[dict]:
    return [
        item for item in time_prefs
        if item.get("enabled")
        and item.get("fanout", 0) > 0
        and item.get("time") not in occupied
    ]


def collect_query_slot_times(profile: dict) -> list[str]:
    times: list[str] = []
    seen: set[str] = set()

    def add(time_str: str | None):
        if time_str and time_str not in seen:
            times.append(time_str)
            seen.add(time_str)

    for item in profile.get("time_prefs") or []:
        if item.get("enabled") and item.get("fanout", 0) > 0:
            add(item.get("time"))
    for item in get_dual_slot_prefs(profile):
        add(item["time"])
        add(item["next_time"])
    return times


def pick_dual_pairs(first_candidates: list[dict], second_candidates: list[dict]) -> list[tuple[dict, dict]]:
    """Same venue + same court number only. Missing either slot drops that court."""
    second_index = {}
    for court in second_candidates:
        key = (court["venueId"], court["court"]["courtNumber"])
        if key not in second_index:
            second_index[key] = court

    pairs = []
    for first in first_candidates:
        key = (first["venueId"], first["court"]["courtNumber"])
        second = second_index.get(key)
        if not second:
            continue
        pairs.append((first, second))
    pairs.sort(key=lambda pair: (pair[0].get("priority", 99), pair[1].get("priority", 99)))
    return pairs


def collect_priority_levels(dual_prefs: list[dict], single_prefs: list[dict]) -> list[int]:
    levels = {item.get("priority", 1) for item in dual_prefs}
    levels.update(item.get("priority", 1) for item in single_prefs)
    return sorted(levels)


def tasks_for_wave(priority: int, dual_prefs: list[dict], single_prefs: list[dict],
                   dual_road_succeeded: bool) -> tuple[list[dict], list[dict]]:
    duals = [] if dual_road_succeeded else [
        item for item in dual_prefs if item.get("priority", 1) == priority
    ]
    singles = [item for item in single_prefs if item.get("priority", 1) == priority]
    return duals, singles

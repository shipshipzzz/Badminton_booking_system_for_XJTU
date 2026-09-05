"""Candidate pool: the courts a profile is willing to book, kept ready before T.

The pool starts as *every selected court* for every queried time slot. Each
successful inventory query then fills in the server IDs (stockId / seatId) and
marks courts explicitly unavailable. A failed or partial query never shrinks
the pool: venues that did not answer keep their last known state.

Pure functions, no I/O.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from models import VENUES

VENUE_COURT_COUNTS = {v["id"]: v["totalCourts"] for v in VENUES}


# ==================== Slot matching ====================

def slot_start_minutes(time_slot: str) -> int | None:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-", str(time_slot or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute


def find_matching_slot(times: list[dict], preferred_time: str) -> dict | None:
    """Exact TIME_NO match first, otherwise the closest slot starting in the same hour."""
    exact = next((t for t in times if t.get("TIME_NO") == preferred_time), None)
    if exact:
        return exact

    preferred_start = slot_start_minutes(preferred_time)
    if preferred_start is None:
        return None

    same_hour_candidates = []
    for slot in times:
        slot_start = slot_start_minutes(slot.get("TIME_NO", ""))
        if slot_start is None:
            continue
        diff = abs(slot_start - preferred_start)
        if diff <= 45 and slot_start // 60 == preferred_start // 60:
            same_hour_candidates.append((diff, slot_start, slot))

    if not same_hour_candidates:
        return None
    same_hour_candidates.sort(key=lambda item: (item[0], item[1]))
    return same_hour_candidates[0][2]


# ==================== Pool model ====================

@dataclass
class CandidatePool:
    date: str
    slot_times: list[str]
    entries: dict[str, list[dict]]
    created_at: float
    cutoff_at: float
    # Venues whose selection is "all courts": courts seen in a query are added on the fly.
    any_court_venues: set[int] = field(default_factory=set)
    venue_applied_at: dict[int, float] = field(default_factory=dict)
    last_success_at: float | None = None
    last_success_source: str = ""
    last_success_seq: int | None = None


def _venue_priority(vp: dict, fallback: int) -> int:
    raw = vp.get("priority")
    try:
        value = fallback if raw is None or raw == "" else int(raw)
    except (TypeError, ValueError):
        value = fallback
    return max(1, min(value, 5))


def _court_priority(vp: dict, court_number: int) -> int:
    raw = (vp.get("courtPriority") or {}).get(str(court_number), court_number)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return court_number


def _sort_key(entry: dict):
    return (
        entry.get("venuePriority", 99),
        entry.get("priority", 99),
        entry.get("venueId", 0),
        entry["court"]["courtNumber"],
    )


def _make_entry(vp: dict, venue_rank: int, date: str, slot_time: str, court_number: int) -> dict:
    return {
        "venueId": vp["id"],
        "venueName": vp.get("name", str(vp["id"])),
        "venuePriority": _venue_priority(vp, venue_rank),
        "date": date,
        "prefTime": slot_time,
        "timeSlot": slot_time,
        "stockId": None,
        "court": {
            "courtNumber": court_number,
            "courtName": f"场地{court_number}",
            "seatId": None,
        },
        "priority": _court_priority(vp, court_number),
        "available": None,
        "checkedAt": None,
    }


def is_resolved(entry: dict) -> bool:
    return entry.get("stockId") is not None and bool(entry.get("court", {}).get("seatId"))


def is_bookable(entry: dict) -> bool:
    return is_resolved(entry) and entry.get("available") is True


def build_pool(profile: dict, date: str, slot_times: list[str], now: float | None = None) -> CandidatePool:
    """Default pool: every enabled venue x selected court x queried slot, IDs unknown."""
    if now is None:
        now = time.time()
    venue_prefs = [v for v in (profile.get("venue_prefs") or []) if v.get("enabled")]
    entries: dict[str, list[dict]] = {slot_time: [] for slot_time in slot_times}
    any_court_venues: set[int] = set()

    for rank, vp in enumerate(venue_prefs, start=1):
        selected = []
        for raw in vp.get("courts") or []:
            try:
                selected.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not selected:
            any_court_venues.add(vp["id"])
            selected = list(range(1, VENUE_COURT_COUNTS.get(vp["id"], 0) + 1))
        for slot_time in slot_times:
            for court_number in sorted(set(selected)):
                entries[slot_time].append(_make_entry(vp, rank, date, slot_time, court_number))

    for slot_time in slot_times:
        entries[slot_time].sort(key=_sort_key)

    return CandidatePool(
        date=date,
        slot_times=list(slot_times),
        entries=entries,
        created_at=now,
        cutoff_at=now,
        any_court_venues=any_court_venues,
    )


def rebuild_pool(old: CandidatePool | None, profile: dict, date: str, slot_times: list[str],
                 now: float | None = None) -> CandidatePool:
    """Rebuild membership from the current selection, keeping known IDs for the same date."""
    new = build_pool(profile, date, slot_times, now=now)
    if old is None or old.date != date:
        return new

    index: dict[tuple, dict] = {}
    for slot_time, old_entries in old.entries.items():
        for entry in old_entries:
            index[(slot_time, entry["venueId"], entry["court"]["courtNumber"])] = entry

    for slot_time, new_entries in new.entries.items():
        seen: set[tuple] = set()
        for entry in new_entries:
            key = (slot_time, entry["venueId"], entry["court"]["courtNumber"])
            seen.add(key)
            previous = index.get(key)
            if previous:
                _copy_resolution(previous, entry)
        # Courts discovered on the fly for "all courts" venues survive a rebuild.
        for key, previous in index.items():
            if key[0] != slot_time or key in seen or key[1] not in new.any_court_venues:
                continue
            vp = next((v for v in profile.get("venue_prefs") or [] if v.get("id") == key[1]), None)
            if not vp or not vp.get("enabled"):
                continue
            rank = previous.get("venuePriority", 1)
            entry = _make_entry(vp, rank, date, slot_time, key[2])
            _copy_resolution(previous, entry)
            new_entries.append(entry)
        new_entries.sort(key=_sort_key)

    new.cutoff_at = old.cutoff_at
    new.created_at = old.created_at
    new.venue_applied_at = dict(old.venue_applied_at)
    new.last_success_at = old.last_success_at
    new.last_success_source = old.last_success_source
    new.last_success_seq = old.last_success_seq
    return new


def _copy_resolution(source: dict, target: dict) -> None:
    target["stockId"] = source.get("stockId")
    target["timeSlot"] = source.get("timeSlot") or target["timeSlot"]
    target["court"]["seatId"] = source.get("court", {}).get("seatId")
    target["available"] = source.get("available")
    target["checkedAt"] = source.get("checkedAt")


# ==================== Merging query results ====================

def merge_venue_results(pool: CandidatePool, venue_results: dict[int, dict], started_at: float,
                        seq: int | None = None, source: str = "live",
                        venue_prefs: list[dict] | None = None, now: float | None = None) -> dict:
    """Apply one query's per-venue results to the pool.

    venue_results[venue_id] = {
        "ok": bool,                      # False -> venue untouched
        "times": [slot dicts with ID/TIME_NO],
        "seats_by_stock": {str(stock_id): [seat dicts] | None},  # None -> that slot untouched
    }
    Results that started before the pool cutoff, or before the last result applied to
    that venue, are discarded as stale.
    """
    if now is None:
        now = time.time()
    stats = {
        "applied": [],
        "failed": [],
        "stale": [],
        "resolved": 0,
        "removed": [],
        "restored": [],
        "added": [],
    }
    prefs_by_id = {v.get("id"): v for v in (venue_prefs or [])}

    for venue_id, result in venue_results.items():
        if not result or not result.get("ok"):
            stats["failed"].append(venue_id)
            continue
        if started_at < pool.cutoff_at or started_at < pool.venue_applied_at.get(venue_id, 0.0):
            stats["stale"].append(venue_id)
            continue
        pool.venue_applied_at[venue_id] = started_at
        stats["applied"].append(venue_id)

        times = result.get("times") or []
        seats_by_stock = result.get("seats_by_stock") or {}

        for slot_time in pool.slot_times:
            slot_entries = pool.entries.setdefault(slot_time, [])
            venue_entries = [e for e in slot_entries if e["venueId"] == venue_id]
            slot = find_matching_slot(times, slot_time)
            if slot is None:
                # The venue answered and does not offer this slot at all.
                for entry in venue_entries:
                    _mark_unavailable(entry, now, stats, slot_time)
                continue
            seats = seats_by_stock.get(str(slot["ID"]))
            if seats is None:
                continue  # seat fetch for this slot failed: keep last known state
            seat_map = {s["courtNumber"]: s for s in seats}
            seen_courts: set[int] = set()
            for entry in venue_entries:
                court_number = entry["court"]["courtNumber"]
                seen_courts.add(court_number)
                seat = seat_map.get(court_number)
                if seat is None:
                    _mark_unavailable(entry, now, stats, slot_time)
                else:
                    _resolve(entry, slot, seat, now, stats, slot_time)
            if venue_id in pool.any_court_venues:
                vp = prefs_by_id.get(venue_id)
                if vp is None:
                    vp = {"id": venue_id, "name": venue_entries[0]["venueName"] if venue_entries else str(venue_id)}
                rank = venue_entries[0].get("venuePriority", 1) if venue_entries else 1
                added = False
                for court_number in sorted(seat_map):
                    if court_number in seen_courts:
                        continue
                    entry = _make_entry(vp, rank, pool.date, slot_time, court_number)
                    _resolve(entry, slot, seat_map[court_number], now, stats, slot_time)
                    slot_entries.append(entry)
                    stats["added"].append(f"{entry['venueName']} {entry['court']['courtName']}@{slot_time}")
                    added = True
                if added:
                    slot_entries.sort(key=_sort_key)

    if stats["applied"]:
        pool.last_success_at = now
        pool.last_success_source = source
        pool.last_success_seq = seq
    return stats


def _label(entry: dict, slot_time: str) -> str:
    return f"{entry['venueName']} {entry['court']['courtName']}@{slot_time}"


def _mark_unavailable(entry: dict, now: float, stats: dict, slot_time: str) -> None:
    was_bookable = is_bookable(entry)
    entry["available"] = False
    entry["checkedAt"] = now
    if was_bookable:
        stats["removed"].append(_label(entry, slot_time))


def _resolve(entry: dict, slot: dict, seat: dict, now: float, stats: dict, slot_time: str) -> None:
    was_bookable = is_bookable(entry)
    was_resolved = is_resolved(entry)
    entry["stockId"] = slot["ID"]
    entry["timeSlot"] = slot.get("TIME_NO") or entry.get("timeSlot") or slot_time
    entry["court"]["seatId"] = str(seat["seatId"])
    entry["available"] = bool(seat.get("available"))
    entry["checkedAt"] = now
    now_bookable = is_bookable(entry)
    if not was_resolved:
        stats["resolved"] += 1
    if was_bookable and not now_bookable:
        stats["removed"].append(_label(entry, slot_time))
    elif was_resolved and not was_bookable and now_bookable:
        stats["restored"].append(_label(entry, slot_time))


# ==================== Reading the pool ====================

def _copy_entry(entry: dict) -> dict:
    copied = dict(entry)
    copied["court"] = dict(entry["court"])
    return copied


def bookable_entries(pool: CandidatePool | None, slot_time: str) -> list[dict]:
    if pool is None:
        return []
    return [_copy_entry(e) for e in pool.entries.get(slot_time, []) if is_bookable(e)]


def pool_summary(pool: CandidatePool | None) -> dict | None:
    if pool is None:
        return None
    selected = bookable = unresolved = unavailable = 0
    slots: dict[str, int] = {}
    for slot_time in pool.slot_times:
        count = 0
        for entry in pool.entries.get(slot_time, []):
            selected += 1
            if not is_resolved(entry):
                unresolved += 1
            elif is_bookable(entry):
                bookable += 1
                count += 1
            else:
                unavailable += 1
        slots[slot_time] = count
    return {
        "date": pool.date,
        "selected": selected,
        "bookable": bookable,
        "unresolved": unresolved,
        "unavailable": unavailable,
        "slots": slots,
        "last_success_at": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pool.last_success_at))
            if pool.last_success_at else None
        ),
        "last_success_source": pool.last_success_source or None,
    }


def describe_slots(pool: CandidatePool | None) -> list[str]:
    """One line per slot listing bookable courts in priority order."""
    if pool is None:
        return []
    lines = []
    for slot_time in pool.slot_times:
        courts = bookable_entries(pool, slot_time)
        if not courts:
            continue
        court_list = ", ".join(
            f"{c['venueName']} {c['court']['courtName']}(馆{c.get('venuePriority', '?')}/场{c.get('priority', '?')})"
            for c in courts
        )
        lines.append(f"{slot_time}: {court_list}")
    return lines


def bookable_signature(pool: CandidatePool | None) -> tuple:
    if pool is None:
        return ()
    return tuple(
        (slot_time, tuple((e["venueId"], e["court"]["courtNumber"]) for e in bookable_entries(pool, slot_time)))
        for slot_time in pool.slot_times
    )

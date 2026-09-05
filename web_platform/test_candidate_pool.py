#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the candidate pool (default membership, merging, staleness)."""

import os
import sys
import unittest

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
sys.path.insert(0, os.path.join(os.path.dirname(_here), "cas_http"))

from candidate_pool import (
    bookable_entries,
    build_pool,
    find_matching_slot,
    is_bookable,
    is_resolved,
    merge_venue_results,
    pool_summary,
    rebuild_pool,
)

SLOTS = ["19:31-20:30", "20:31-21:30"]


def _profile(courts_104=(3, 1), courts_103=(), enabled_103=True):
    return {
        "venue_prefs": [
            {"id": 104, "name": "三号巨构", "enabled": True, "priority": 1,
             "courts": list(courts_104), "courtPriority": {"3": 1, "1": 2, "2": 3}},
            {"id": 103, "name": "二号巨构", "enabled": enabled_103, "priority": 2,
             "courts": list(courts_103), "courtPriority": {}},
        ],
        "time_prefs": [{"time": t, "enabled": True, "fanout": 4} for t in SLOTS],
    }


def _seat(court, seat_id, available=True):
    return {"courtNumber": court, "seatId": str(seat_id), "available": available}


def _venue_ok(stock_by_slot, seats_by_stock):
    return {
        "ok": True,
        "times": [{"ID": stock, "TIME_NO": slot} for slot, stock in stock_by_slot.items()],
        "seats_by_stock": {str(k): v for k, v in seats_by_stock.items()},
    }


class BuildPoolTests(unittest.TestCase):
    def test_default_pool_contains_every_selected_court_without_ids(self):
        pool = build_pool(_profile(), "2026-09-06", SLOTS, now=100.0)
        for slot in SLOTS:
            entries = pool.entries[slot]
            # 104: courts 3,1 selected; 103: no explicit selection -> all 6 courts
            self.assertEqual(len(entries), 2 + 6)
            self.assertTrue(all(not is_resolved(e) for e in entries))
            self.assertTrue(all(e["available"] is None for e in entries))
        self.assertEqual(pool.any_court_venues, {103})
        self.assertEqual(bookable_entries(pool, SLOTS[0]), [])

    def test_default_pool_is_sorted_by_venue_then_court_priority(self):
        pool = build_pool(_profile(), "2026-09-06", SLOTS, now=100.0)
        first_two = [(e["venueId"], e["court"]["courtNumber"]) for e in pool.entries[SLOTS[0]][:2]]
        self.assertEqual(first_two, [(104, 3), (104, 1)])

    def test_disabled_venue_is_excluded(self):
        pool = build_pool(_profile(enabled_103=False), "2026-09-06", SLOTS, now=100.0)
        self.assertEqual({e["venueId"] for e in pool.entries[SLOTS[0]]}, {104})


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.profile = _profile(enabled_103=False)
        self.pool = build_pool(self.profile, "2026-09-06", SLOTS, now=100.0)
        self.result_104 = {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: [_seat(1, 4322228), _seat(2, 4322229), _seat(3, 4322230)],
             448697: [_seat(1, 4322231), _seat(2, 4322232), _seat(3, 4322233)]},
        )}

    def test_successful_query_fills_ids_and_marks_available(self):
        stats = merge_venue_results(self.pool, self.result_104, started_at=101.0, seq=1, source="T-12h", now=102.0)
        self.assertEqual(stats["applied"], [104])
        self.assertEqual(stats["resolved"], 4)
        bookable = bookable_entries(self.pool, "19:31-20:30")
        self.assertEqual([(e["court"]["courtNumber"], e["stockId"], e["court"]["seatId"]) for e in bookable],
                         [(3, 448696, "4322230"), (1, 448696, "4322228")])
        self.assertEqual(self.pool.last_success_source, "T-12h")
        self.assertEqual(self.pool.last_success_at, 102.0)
        summary = pool_summary(self.pool)
        self.assertEqual((summary["selected"], summary["bookable"], summary["unresolved"]), (4, 4, 0))

    def test_failed_venue_keeps_previous_state(self):
        merge_venue_results(self.pool, self.result_104, started_at=101.0, seq=1, now=102.0)
        stats = merge_venue_results(self.pool, {104: {"ok": False}}, started_at=200.0, seq=2, now=201.0)
        self.assertEqual(stats["failed"], [104])
        self.assertEqual(len(bookable_entries(self.pool, "19:31-20:30")), 2)
        self.assertEqual(self.pool.last_success_at, 102.0)  # unchanged

    def test_booked_court_is_removed_and_can_come_back(self):
        merge_venue_results(self.pool, self.result_104, started_at=101.0, seq=1, now=102.0)
        taken = {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: [_seat(1, 4322228), _seat(2, 4322229), _seat(3, 4322230, available=False)],
             448697: [_seat(1, 4322231), _seat(2, 4322232), _seat(3, 4322233)]},
        )}
        stats = merge_venue_results(self.pool, taken, started_at=200.0, seq=2, now=201.0)
        self.assertEqual(stats["removed"], ["三号巨构 场地3@19:31-20:30"])
        self.assertEqual([e["court"]["courtNumber"] for e in bookable_entries(self.pool, "19:31-20:30")], [1])
        stats = merge_venue_results(self.pool, self.result_104, started_at=300.0, seq=3, now=301.0)
        self.assertEqual(stats["restored"], ["三号巨构 场地3@19:31-20:30"])
        self.assertEqual(len(bookable_entries(self.pool, "19:31-20:30")), 2)

    def test_slot_not_offered_marks_venue_courts_unavailable(self):
        merge_venue_results(self.pool, self.result_104, started_at=101.0, seq=1, now=102.0)
        only_first = {104: _venue_ok(
            {"19:31-20:30": 448696},
            {448696: [_seat(1, 4322228), _seat(3, 4322230)]},
        )}
        merge_venue_results(self.pool, only_first, started_at=200.0, seq=2, now=201.0)
        self.assertEqual(len(bookable_entries(self.pool, "19:31-20:30")), 2)
        self.assertEqual(bookable_entries(self.pool, "20:31-21:30"), [])
        # IDs from the earlier query are kept even though the slot is now unavailable
        second = self.pool.entries["20:31-21:30"][0]
        self.assertTrue(is_resolved(second))
        self.assertFalse(is_bookable(second))

    def test_failed_seat_fetch_leaves_slot_untouched(self):
        merge_venue_results(self.pool, self.result_104, started_at=101.0, seq=1, now=102.0)
        partial = {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: None, 448697: [_seat(1, 4322231, available=False), _seat(3, 4322233, available=False)]},
        )}
        merge_venue_results(self.pool, partial, started_at=200.0, seq=2, now=201.0)
        self.assertEqual(len(bookable_entries(self.pool, "19:31-20:30")), 2)
        self.assertEqual(bookable_entries(self.pool, "20:31-21:30"), [])

    def test_stale_result_started_before_applied_one_is_discarded(self):
        merge_venue_results(self.pool, self.result_104, started_at=101.0, seq=2, now=102.0)
        older = {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: [_seat(1, 4322228, available=False), _seat(3, 4322230, available=False)],
             448697: [_seat(1, 4322231, available=False), _seat(3, 4322233, available=False)]},
        )}
        stats = merge_venue_results(self.pool, older, started_at=100.5, seq=1, now=103.0)
        self.assertEqual(stats["stale"], [104])
        self.assertEqual(len(bookable_entries(self.pool, "19:31-20:30")), 2)

    def test_result_started_before_pool_cutoff_is_discarded(self):
        stats = merge_venue_results(self.pool, self.result_104, started_at=99.0, seq=1, now=103.0)
        self.assertEqual(stats["stale"], [104])
        self.assertEqual(bookable_entries(self.pool, "19:31-20:30"), [])

    def test_any_court_venue_adds_courts_seen_in_query(self):
        profile = _profile(courts_104=(), enabled_103=False)
        pool = build_pool(profile, "2026-09-06", SLOTS, now=100.0)
        self.assertEqual(len(pool.entries[SLOTS[0]]), 3)  # totalCourts for 104
        result = {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: [_seat(1, 1), _seat(2, 2), _seat(3, 3), _seat(4, 4)],
             448697: [_seat(1, 5), _seat(2, 6), _seat(3, 7), _seat(4, 8)]},
        )}
        stats = merge_venue_results(pool, result, started_at=101.0, seq=1, now=102.0,
                                    venue_prefs=profile["venue_prefs"])
        self.assertEqual(len(stats["added"]), 2)
        # court 3 has priority 1 in the fixture; court 4 was added on the fly with its number as priority
        self.assertEqual([e["court"]["courtNumber"] for e in bookable_entries(pool, "19:31-20:30")], [3, 1, 2, 4])


class RebuildTests(unittest.TestCase):
    def test_rebuild_keeps_ids_for_same_date_and_drops_deselected_courts(self):
        profile = _profile(enabled_103=False)
        pool = build_pool(profile, "2026-09-06", SLOTS, now=100.0)
        merge_venue_results(pool, {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: [_seat(1, 4322228), _seat(3, 4322230)], 448697: [_seat(1, 4322231), _seat(3, 4322233)]},
        )}, started_at=101.0, seq=1, source="T-12h", now=102.0)

        changed = _profile(courts_104=(3, 2), enabled_103=False)
        rebuilt = rebuild_pool(pool, changed, "2026-09-06", SLOTS, now=300.0)
        courts = {(e["court"]["courtNumber"], is_resolved(e)) for e in rebuilt.entries["19:31-20:30"]}
        self.assertEqual(courts, {(3, True), (2, False)})
        self.assertEqual(rebuilt.last_success_source, "T-12h")
        self.assertEqual(rebuilt.cutoff_at, 100.0)
        self.assertEqual(rebuilt.venue_applied_at, {104: 101.0})

    def test_rebuild_for_new_date_starts_fresh(self):
        profile = _profile(enabled_103=False)
        pool = build_pool(profile, "2026-09-06", SLOTS, now=100.0)
        merge_venue_results(pool, {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: [_seat(1, 4322228), _seat(3, 4322230)], 448697: [_seat(1, 4322231), _seat(3, 4322233)]},
        )}, started_at=101.0, seq=1, now=102.0)
        rebuilt = rebuild_pool(pool, profile, "2026-09-07", SLOTS, now=300.0)
        self.assertEqual(rebuilt.date, "2026-09-07")
        self.assertTrue(all(not is_resolved(e) for e in rebuilt.entries["19:31-20:30"]))
        self.assertIsNone(rebuilt.last_success_at)
        self.assertEqual(rebuilt.cutoff_at, 300.0)


class SlotMatchTests(unittest.TestCase):
    def test_exact_then_same_hour_fallback(self):
        times = [{"ID": 1, "TIME_NO": "19:30-20:30"}, {"ID": 2, "TIME_NO": "20:31-21:30"}]
        self.assertEqual(find_matching_slot(times, "20:31-21:30")["ID"], 2)
        self.assertEqual(find_matching_slot(times, "19:31-20:30")["ID"], 1)
        self.assertIsNone(find_matching_slot(times, "18:31-19:30"))


class EngineIntegrationTests(unittest.TestCase):
    def test_copy_slot_candidates_reads_bookable_pool_entries(self):
        from booking_engine import ProfileRuntime, ensure_pool, copy_slot_candidates

        rt = ProfileRuntime(profile_id=1)
        profile = _profile(enabled_103=False)
        pool = ensure_pool(rt, profile, "2026-09-06")
        self.assertEqual(pool.slot_times, SLOTS)
        self.assertEqual(copy_slot_candidates(rt, *SLOTS), [[], []])
        merge_venue_results(rt.pool, {104: _venue_ok(
            {"19:31-20:30": 448696, "20:31-21:30": 448697},
            {448696: [_seat(1, 4322228), _seat(3, 4322230)], 448697: [_seat(1, 4322231), _seat(3, 4322233)]},
        )}, started_at=pool.cutoff_at + 1, seq=1)
        first, second = copy_slot_candidates(rt, *SLOTS)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        # Copies: mutating the snapshot must not touch the pool
        first[0]["court"]["seatId"] = "x"
        self.assertNotEqual(rt.pool.entries[SLOTS[0]][0]["court"]["seatId"], "x")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for dual-slot pairing and order payload helpers."""

import os
import sys
import unittest

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
sys.path.insert(0, os.path.join(os.path.dirname(_here), "cas_http"))

from dual_slot import (
    collect_query_slot_times,
    dual_occupied_times,
    filter_single_time_prefs,
    get_dual_slot_prefs,
    next_slot_time,
    pick_dual_pairs,
    tasks_for_wave,
)
from booking_api import _order_param
from main import _summarize_order_detail_rows


def _court(venue_id, court_number, time_slot, stock_id, seat_id, priority=1, venue_name="二号巨构"):
    return {
        "venueId": venue_id,
        "venueName": venue_name,
        "timeSlot": time_slot,
        "stockId": stock_id,
        "priority": priority,
        "court": {
            "courtNumber": court_number,
            "courtName": f"场地{court_number}",
            "seatId": seat_id,
        },
    }


class DualSlotHelperTests(unittest.TestCase):
    def test_next_slot_follows_table(self):
        self.assertEqual(next_slot_time("17:31-18:30"), "18:31-19:30")
        self.assertEqual(next_slot_time("11:01-12:00"), "14:30-15:30")
        self.assertIsNone(next_slot_time("20:31-21:30"))
        self.assertIsNone(next_slot_time("missing"))

    def test_dual_prefs_inherit_fanout_and_skip_last_slot(self):
        profile = {
            "dual_slot_enabled": True,
            "dual_slot_prefs": [
                {"time": "17:31-18:30", "priority": 1},
                {"time": "20:31-21:30", "priority": 2},
            ],
            "time_prefs": [
                {"time": "17:31-18:30", "enabled": False, "fanout": 6, "parallel": 2, "priority": 3},
            ],
        }
        prefs = get_dual_slot_prefs(profile)
        self.assertEqual(len(prefs), 1)
        self.assertEqual(prefs[0]["next_time"], "18:31-19:30")
        self.assertEqual(prefs[0]["fanout"], 6)
        self.assertEqual(prefs[0]["parallel"], 2)
        self.assertEqual(prefs[0]["priority"], 1)

    def test_disabled_switch_returns_empty(self):
        profile = {
            "dual_slot_enabled": False,
            "dual_slot_prefs": [{"time": "17:31-18:30", "priority": 1}],
        }
        self.assertEqual(get_dual_slot_prefs(profile), [])

    def test_single_road_excludes_occupied_slots(self):
        duals = get_dual_slot_prefs({
            "dual_slot_enabled": True,
            "dual_slot_prefs": [{"time": "17:31-18:30", "priority": 1}],
            "time_prefs": [],
        })
        occupied = dual_occupied_times(duals)
        self.assertEqual(occupied, {"17:31-18:30", "18:31-19:30"})
        singles = filter_single_time_prefs([
            {"time": "16:31-17:30", "enabled": True, "fanout": 4, "priority": 1},
            {"time": "17:31-18:30", "enabled": True, "fanout": 4, "priority": 1},
            {"time": "18:31-19:30", "enabled": True, "fanout": 4, "priority": 2},
        ], occupied)
        self.assertEqual([item["time"] for item in singles], ["16:31-17:30"])

    def test_query_includes_dual_next_even_if_unchecked(self):
        profile = {
            "dual_slot_enabled": True,
            "dual_slot_prefs": [{"time": "17:31-18:30", "priority": 1}],
            "time_prefs": [
                {"time": "16:31-17:30", "enabled": True, "fanout": 4},
                {"time": "17:31-18:30", "enabled": False, "fanout": 4},
                {"time": "18:31-19:30", "enabled": False, "fanout": 4},
            ],
        }
        self.assertEqual(
            collect_query_slot_times(profile),
            ["16:31-17:30", "17:31-18:30", "18:31-19:30"],
        )

    def test_pick_pairs_requires_same_venue_and_court(self):
        first = [
            _court(103, 3, "17:31-18:30", "s1", "a", priority=1),
            _court(103, 4, "17:31-18:30", "s1", "b", priority=2),
            _court(101, 3, "17:31-18:30", "s9", "c", priority=1),
        ]
        second = [
            _court(103, 3, "18:31-19:30", "s2", "d", priority=1),
            _court(101, 1, "18:31-19:30", "s8", "e", priority=1),
        ]
        pairs = pick_dual_pairs(first, second)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0]["court"]["courtNumber"], 3)
        self.assertEqual(pairs[0][0]["venueId"], 103)
        self.assertEqual(pairs[0][1]["stockId"], "s2")

    def test_wave_skips_dual_after_success(self):
        duals = [
            {"time": "17:31-18:30", "next_time": "18:31-19:30", "priority": 1},
            {"time": "14:30-15:30", "next_time": "15:31-16:30", "priority": 2},
        ]
        singles = [
            {"time": "16:31-17:30", "priority": 2},
        ]
        wave1_duals, wave1_singles = tasks_for_wave(1, duals, singles, False)
        self.assertEqual(len(wave1_duals), 1)
        self.assertEqual(wave1_singles, [])

        wave2_after_fail, singles_after_fail = tasks_for_wave(2, duals, singles, False)
        self.assertEqual(len(wave2_after_fail), 1)
        self.assertEqual(len(singles_after_fail), 1)

        wave2_after_ok, singles_after_ok = tasks_for_wave(2, duals, singles, True)
        self.assertEqual(wave2_after_ok, [])
        self.assertEqual(len(singles_after_ok), 1)


class OrderDetailSummaryTests(unittest.TestCase):
    def test_dual_order_shows_both_slots(self):
        summary = _summarize_order_detail_rows([
            {
                "stock": {"s_date": "2026-08-18", "time_no": "15:31-16:30"},
                "stockdetail": {"sname": "场地1"},
            },
            {
                "stock": {"s_date": "2026-08-18", "time_no": "14:30-15:30"},
                "stockdetail": {"sname": "场地1"},
            },
        ])
        self.assertEqual(summary["date"], "2026-08-18")
        self.assertEqual(summary["timeSlot"], "14:30-15:30\n15:31-16:30")
        self.assertEqual(summary["court"], "场地1")

    def test_single_order_unchanged(self):
        summary = _summarize_order_detail_rows([
            {
                "stock": {"s_date": "2026-08-18", "time_no": "14:30-15:30"},
                "stockdetail": {"sname": "场地1"},
            },
        ])
        self.assertEqual(summary["timeSlot"], "14:30-15:30")
        self.assertEqual(summary["court"], "场地1")


class OrderParamTests(unittest.TestCase):
    def test_single_item_payload(self):
        param = _order_param(103, [{"stock_id": "445080", "seat_id": "seatA"}], for_booking=True)
        self.assertEqual(param["stock"], {"445080": "1"})
        self.assertEqual(param["stockdetail"], {"445080": "seatA"})
        self.assertEqual(param["stockdetailids"], "seatA")
        self.assertEqual(param["address"], "103")

    def test_dual_item_payload(self):
        param = _order_param(103, [
            {"stock_id": "445080", "seat_id": "seatA"},
            {"stock_id": "445081", "seat_id": "seatB"},
        ], for_booking=True)
        self.assertEqual(param["stock"], {"445080": "1", "445081": "1"})
        self.assertEqual(param["stockdetail"], {"445080": "seatA", "445081": "seatB"})
        self.assertEqual(param["stockdetailids"], "seatA,seatB")


if __name__ == "__main__":
    unittest.main()

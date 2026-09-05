#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HTTP helpers for the XJTU badminton booking system (8080 H5 or port-80 PC)."""

import json
import math
import random
import re
import time
from urllib.parse import urlencode

import httpx

from site_config import get_channel
from http_capture import record_request


AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _cfg(channel=None, client=None):
    if channel is None and client is not None:
        channel = getattr(client, "booking_channel", None)
    return get_channel(channel)


def _headers(cfg):
    return {"X-Requested-With": "XMLHttpRequest", "User-Agent": cfg.user_agent}


def _get_with_retry(client, url, headers=None, retries=4, delay=0.35):
    """Official site often returns empty HTTP 500; retry before giving up.

    A redirect (e.g. the :8080 channel sending anonymous queries to errorpage.html
    outside 08:40-21:40) is deterministic, so it is returned at once without retrying.
    """
    last = None
    for attempt in range(retries):
        last = record_request(client, "GET", url, capture_meta={"attempt": attempt + 1},
                              headers=headers or AJAX_HEADERS, timeout=10)
        if last.status_code == 200 and last.content:
            return last
        if 300 <= last.status_code < 400:
            return last
        time.sleep(delay * (attempt + 1))
    return last


def fetch_ok_area(client, venue_id, date, channel=None):
    """Always hit the official API. No cross-query cache."""
    cfg = _cfg(channel, client)
    url = cfg.api_url(f"/product/findOkArea.html?s_date={date}&serviceid={venue_id}&_={int(time.time()*1000)}")
    resp = _get_with_retry(client, url, headers=_headers(cfg))
    if resp is None or resp.status_code != 200 or not resp.content:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    rows = data.get("object") or []
    if not isinstance(rows, list):
        return []
    return rows


def _times_from_ok_area(rows):
    by_stock = {}
    for row in rows or []:
        stock_id = row.get("stockid")
        if stock_id is None:
            continue
        stock = row.get("stock") or {}
        entry = by_stock.setdefault(str(stock_id), {
            "ID": stock_id,
            "TIME_NO": stock.get("time_no") or "",
            "SURPLUS": 0,
            "ALL_COUNT": int(stock.get("all_count") or 0),
        })
        if row.get("status") == 1:
            entry["SURPLUS"] += 1
        entry["ALL_COUNT"] = max(int(entry["ALL_COUNT"] or 0), int(entry["SURPLUS"]), int(row.get("num") or 0))
    return sorted(by_stock.values(), key=lambda item: item.get("TIME_NO") or "")


def _query_times_findtime(client, cfg, venue_id, date):
    url = cfg.api_url(f"/product/findtime.html?type=day&s_dates={date}&serviceid={venue_id}&_={int(time.time()*1000)}")
    resp = _get_with_retry(client, url, headers=_headers(cfg))
    if resp is None or resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    if data.get("result") == "1":
        return data.get("object", [])
    return []


def query_times(client, venue_id, date, channel=None):
    cfg = _cfg(channel, client)
    if cfg.seat_mode == "ok_area":
        rows = fetch_ok_area(client, venue_id, date, cfg.id)
        times = _times_from_ok_area(rows)
        if times:
            return times
    return _query_times_findtime(client, cfg, venue_id, date)


def _slot_payload(slot, seats):
    return {
        "time": slot["TIME_NO"],
        "stockId": slot["ID"],
        "surplus": int(slot.get("SURPLUS") or 0),
        "allCount": int(slot.get("ALL_COUNT") or 0),
        "seats": seats,
    }


def query_venue_occupancy(client, venue_id, date, channel=None):
    """One official occupancy fetch per venue, then split times/seats locally."""
    cfg = _cfg(channel, client)
    if cfg.seat_mode == "ok_area":
        rows = fetch_ok_area(client, venue_id, date, cfg.id)
        if rows is None:
            times = _query_times_findtime(client, cfg, venue_id, date)
            return [_slot_payload(slot, []) for slot in times]
        return [
            _slot_payload(slot, seats_from_ok_area(rows, slot["ID"]))
            for slot in _times_from_ok_area(rows)
        ]
    times = _query_times_findtime(client, cfg, venue_id, date)
    return [
        _slot_payload(slot, _query_seats_html(client, cfg, venue_id, slot["ID"]))
        for slot in times
    ]


def seats_from_ok_area(rows, stock_id) -> list:
    seats = []
    for row in rows or []:
        if str(row.get("stockid")) != str(stock_id):
            continue
        try:
            court_number = int(row.get("name") or row.get("num"))
        except (TypeError, ValueError):
            continue
        seats.append({
            "courtNumber": court_number,
            "seatId": str(row.get("id")),
            "available": row.get("status") == 1,
        })
    return seats


def _query_seats_html(client, cfg, venue_id, stock_id):
    url = cfg.api_url(
        f"/seat/seat.html?id={venue_id}&type=2&stockid={stock_id}&json=html&_={int(time.time()*1000)}"
    )
    resp = _get_with_retry(client, url, headers=_headers(cfg))
    if resp is None or resp.status_code != 200:
        return []
    match = re.search(r'value="([^"]+)"\s+id="txt_seatid"', resp.text)
    if not match:
        return []
    seats = []
    for item in match.group(1).split(","):
        if not item:
            continue
        parts = item.split("_")
        if len(parts) >= 3:
            try:
                court_number = int(parts[0])
            except ValueError:
                continue
            seats.append({
                "courtNumber": court_number,
                "seatId": parts[1],
                "available": parts[-1] == "1",
            })
    return seats


def query_seats(client, venue_id, stock_id, date=None, channel=None):
    cfg = _cfg(channel, client)
    if cfg.seat_mode == "seat_html":
        return _query_seats_html(client, cfg, venue_id, stock_id)
    if not date:
        return []
    return seats_from_ok_area(fetch_ok_area(client, venue_id, date, cfg.id), stock_id)


def fetch_captcha(client, channel=None):
    cfg = _cfg(channel, client)
    resp = record_request(client, "GET", f"{cfg.captcha_url}?_={int(time.time()*1000)}",
                          headers=_headers(cfg), timeout=10)
    data = resp.json()
    captcha_id = data.get("id")
    bg = data.get("captcha", {}).get("backgroundImage")
    slider = data.get("captcha", {}).get("sliderImage")
    if not captcha_id or not bg or not slider:
        raise RuntimeError(f"captcha data incomplete: {list(data.keys())}")
    return captcha_id, bg, slider


def clamp(value, lower, upper):
    return max(lower, min(upper, value))


def randn():
    u, v = 0.0, 0.0
    while u == 0:
        u = random.random()
    while v == 0:
        v = random.random()
    return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)


def ease_in_out_cubic(progress):
    return 4 * progress * progress * progress if progress < 0.5 else 1 - (-2 * progress + 2) ** 3 / 2


def build_human_points(dist_px):
    dist = clamp(round(dist_px), 1, 999)
    steps = clamp(round(dist / 4) + 18, 22, 60)
    duration = clamp(round(dist * 6 + random.uniform(240, 420)), 750, 1500)
    base_t = random.randint(420, 760)
    dt_base = duration / (steps - 1)

    t = base_t
    y = 0.0
    last_x = 0.0
    points = [{"x": 0, "y": 0, "t": t}]

    for i in range(1, steps):
        progress = i / (steps - 1)
        x = dist * ease_in_out_cubic(progress) + random.uniform(-0.6, 0.8)
        if i > 3 and i < steps - 2 and 0.15 < progress < 0.92 and random.random() < 0.08:
            x = max(0, last_x - random.randint(1, 3))
        x = clamp(x, 0, dist)

        sigma = 0.9 + 1.4 * math.sin(math.pi * progress)
        y = y * 0.65 + randn() * sigma * 0.35
        y = clamp(y, -6, 6)

        dt = dt_base + random.uniform(-6, 10)
        if random.random() < 0.06:
            dt += random.randint(70, 200)
        t += max(8, round(dt))

        points.append({"x": round(x), "y": round(y), "t": t})
        last_x = x

    points[-1]["x"] = dist
    return points


def generate_yzm_data(target_x, captcha_id, channel=None):
    points = build_human_points(target_x)
    track_list = [{"x": 0, "y": 0, "type": "down", "t": points[0]["t"]}]
    for point in points[1:]:
        track_list.append({"x": point["x"], "y": point["y"], "type": "move", "t": point["t"]})

    hold = random.randint(120, 260)
    last = points[-1]
    track_list.append({"x": last["x"], "y": last["y"], "type": "up", "t": last["t"] + hold})

    from datetime import datetime, timezone
    start_time = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    yzm_data = {
        "bgImageWidth": 260,
        "bgImageHeight": 159,
        "sliderImageWidth": 45,
        "sliderImageHeight": 159,
        "startSlidingTime": start_time,
        "endSlidingTime": start_time,
        "entSlidingTime": start_time,
        "trackList": track_list,
    }
    return json.dumps(yzm_data) + "synjones" + captcha_id + "synjones" + get_channel(channel).yzm_origin


MAX_ORDER_ITEMS = 2


def _as_order_items(stock_id=None, seat_id=None, items=None):
    if items:
        normalized = []
        for item in items:
            normalized.append({
                "stock_id": item["stock_id"],
                "seat_id": item["seat_id"],
            })
        return normalized[:MAX_ORDER_ITEMS]
    if stock_id is None or seat_id is None:
        raise ValueError("stock_id/seat_id or items required")
    return [{"stock_id": stock_id, "seat_id": seat_id}]


def _order_param(venue_id, items, for_booking=False):
    """Build official order JSON. One POST can carry 1-2 courts."""
    if not items:
        raise ValueError("order items required")
    stock = {}
    stockdetail = {}
    seat_ids = []
    for item in items[:MAX_ORDER_ITEMS]:
        stock_id = str(item["stock_id"])
        seat_id = item["seat_id"]
        stock[stock_id] = str(int(stock.get(stock_id, 0)) + 1)
        stockdetail[stock_id] = seat_id
        seat_ids.append(seat_id)
    param = {
        "stock": stock,
        "address": str(venue_id),
        "stockdetailids": ",".join(str(seat_id) for seat_id in seat_ids),
        "extend": {},
    }
    if for_booking:
        param.update({
            "activityPrice": 0,
            "flag": "0",
            "isbookall": "0",
            "isfreeman": "0",
            "istimes": "0",
            "shoppingcart": "0",
            "subscriber": "0",
            "stockdetail": stockdetail,
            "venueReason": "",
            "fileUrl": "",
        })
    return param


def submit_order(client, venue_id, stock_id=None, seat_id=None, channel=None, items=None):
    cfg = _cfg(channel, client)
    param = _order_param(venue_id, _as_order_items(stock_id, seat_id, items), for_booking=False)
    data = urlencode({"param": json.dumps(param)})
    resp = record_request(client, "POST",
        cfg.api_url(f"/order/show.html?id={venue_id}"),
        content=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg.user_agent,
        },
        timeout=10,
    )
    return resp.status_code, resp.text[:200]


def submit_booking(client, venue_id, stock_id=None, seat_id=None, yzm_data=None, channel=None, items=None):
    if yzm_data is None:
        raise ValueError("yzm_data required")
    cfg = _cfg(channel, client)
    param = _order_param(venue_id, _as_order_items(stock_id, seat_id, items), for_booking=True)
    body = f"param={httpx.URL('?' + urlencode({'p': json.dumps(param)})).params['p']}&yzm={httpx.URL('?' + urlencode({'y': yzm_data})).params['y']}&json=true"
    resp = record_request(client, "POST",
        cfg.api_url(cfg.book_path),
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg.user_agent,
        },
        timeout=10,
    )
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:500]}

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HTTP helpers for the XJTU badminton booking system (8080 H5 or port-80 PC)."""

import json
import math
import random
import re
import threading
import time
from urllib.parse import urlencode

import httpx

from site_config import get_channel, normalize_channel


AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
_OK_AREA_TTL = 2.5
_ok_area_cache: dict = {}
_ok_area_lock = threading.Lock()


def _cfg(channel=None, client=None):
    if channel is None and client is not None:
        channel = getattr(client, "booking_channel", None)
    return get_channel(channel)


def _headers(cfg):
    return {"X-Requested-With": "XMLHttpRequest", "User-Agent": cfg.user_agent}


def _get_with_retry(client, url, headers=None, retries=4, delay=0.35):
    """Official site often returns empty HTTP 500; retry before giving up."""
    last = None
    for attempt in range(retries):
        last = client.get(url, headers=headers or AJAX_HEADERS, timeout=10)
        if last.status_code == 200 and last.content:
            return last
        time.sleep(delay * (attempt + 1))
    return last


def _ok_area_cache_get(channel, venue_id, date):
    key = (normalize_channel(channel), int(venue_id), str(date))
    now = time.time()
    with _ok_area_lock:
        hit = _ok_area_cache.get(key)
        if hit and now - hit[0] < _OK_AREA_TTL:
            return hit[1]
    return None


def _ok_area_cache_put(channel, venue_id, date, rows):
    key = (normalize_channel(channel), int(venue_id), str(date))
    with _ok_area_lock:
        _ok_area_cache[key] = (time.time(), rows)


def _ok_area_cache_by_stock(channel, venue_id, stock_id):
    sid = str(stock_id)
    ch = normalize_channel(channel)
    with _ok_area_lock:
        items = list(_ok_area_cache.items())
    for (cached_ch, cached_venue, _date), (_ts, rows) in items:
        if cached_ch != ch or int(cached_venue) != int(venue_id):
            continue
        if any(str(row.get("stockid")) == sid for row in rows):
            return rows
    return None


def fetch_ok_area(client, venue_id, date, channel=None):
    cfg = _cfg(channel, client)
    cached = _ok_area_cache_get(cfg.id, venue_id, date)
    if cached is not None:
        return cached
    url = cfg.api_url(f"/product/findOkArea.html?s_date={date}&serviceid={venue_id}&_={int(time.time()*1000)}")
    resp = _get_with_retry(client, url, headers=_headers(cfg))
    if resp is None or resp.status_code != 200 or not resp.content:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    rows = data.get("object") or []
    if not isinstance(rows, list):
        return []
    _ok_area_cache_put(cfg.id, venue_id, date, rows)
    return rows


def _times_from_ok_area(rows):
    by_stock = {}
    for row in rows:
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

    rows = fetch_ok_area(client, venue_id, date, cfg.id) if date else None
    if rows is None:
        rows = _ok_area_cache_by_stock(cfg.id, venue_id, stock_id) or []
        if not rows and date:
            rows = fetch_ok_area(client, venue_id, date, cfg.id)

    seats = []
    for row in rows:
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


def fetch_captcha(client, channel=None):
    cfg = _cfg(channel, client)
    resp = client.get(f"{cfg.captcha_url}?_={int(time.time()*1000)}",
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


def submit_order(client, venue_id, stock_id, seat_id, channel=None):
    cfg = _cfg(channel, client)
    param = {
        "stock": {str(stock_id): "1"},
        "address": str(venue_id),
        "stockdetailids": seat_id,
        "extend": {},
    }
    data = urlencode({"param": json.dumps(param)})
    resp = client.post(
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


def submit_booking(client, venue_id, stock_id, seat_id, yzm_data, channel=None):
    cfg = _cfg(channel, client)
    param = {
        "activityPrice": 0,
        "address": str(venue_id),
        "extend": {},
        "flag": "0",
        "isbookall": "0",
        "isfreeman": "0",
        "istimes": "0",
        "shoppingcart": "0",
        "subscriber": "0",
        "stock": {str(stock_id): "1"},
        "stockdetail": {str(stock_id): str(seat_id)},
        "stockdetailids": str(seat_id),
        "venueReason": "",
        "fileUrl": "",
    }
    body = f"param={httpx.URL('?' + urlencode({'p': json.dumps(param)})).params['p']}&yzm={httpx.URL('?' + urlencode({'y': yzm_data})).params['y']}&json=true"
    resp = client.post(
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

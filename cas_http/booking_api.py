#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HTTP helpers for the XJTU badminton booking system."""

import json
import math
import random
import re
import time
from urllib.parse import urlencode

import httpx


HOST = "http://202.117.17.144"
AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def query_times(client, venue_id, date):
    url = f"{HOST}/product/findtime.html?type=day&s_dates={date}&serviceid={venue_id}&_={int(time.time()*1000)}"
    resp = client.get(url, headers=AJAX_HEADERS, timeout=10)
    data = resp.json()
    if data.get("result") == "1":
        return data.get("object", [])
    return []


def query_seats(client, venue_id, stock_id):
    url = f"{HOST}/seat/seat.html?id={venue_id}&type=2&stockid={stock_id}&json=html&_={int(time.time()*1000)}"
    resp = client.get(url, headers=AJAX_HEADERS, timeout=10)
    match = re.search(r'value="([^"]+)"\s+id="txt_seatid"', resp.text)
    if not match:
        return []
    seats = []
    for item in match.group(1).split(","):
        if not item:
            continue
        parts = item.split("_")
        if len(parts) == 3:
            seats.append({
                "courtNumber": int(parts[0]),
                "seatId": parts[1],
                "available": parts[2] == "1",
            })
    return seats


def fetch_captcha(client):
    resp = client.get(f"{HOST}/gen?_={int(time.time()*1000)}",
                      headers=AJAX_HEADERS, timeout=10)
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


def generate_yzm_data(target_x, captcha_id):
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
    return json.dumps(yzm_data) + "synjones" + captcha_id + "synjones" + HOST


def submit_order(client, venue_id, stock_id, seat_id):
    param = {
        "stock": {str(stock_id): "1"},
        "address": str(venue_id),
        "stockdetailids": seat_id,
        "extend": {},
    }
    data = urlencode({"param": json.dumps(param)})
    resp = client.post(
        f"{HOST}/order/show.html?id={venue_id}",
        content=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=10,
    )
    return resp.status_code, resp.text[:200]


def submit_booking(client, venue_id, stock_id, seat_id, yzm_data):
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
        "stockdetail": {str(stock_id): seat_id},
        "stockdetailids": seat_id,
    }
    body = f"param={httpx.URL('?' + urlencode({'p': json.dumps(param)})).params['p']}&yzm={httpx.URL('?' + urlencode({'y': yzm_data})).params['y']}&json=true"
    resp = client.post(
        f"{HOST}/order/book.html",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=10,
    )
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:500]}

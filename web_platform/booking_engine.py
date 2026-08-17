"""
Core booking logic: wraps cas_login + booking_api for per-profile execution.

Timeline:
  T-60s   Login (prepare authenticated session)
  T-6s    Prefetch captchas (store for use at T=0)
  T-Xms   Start querying courts (anonymous, no session, every 1s, max 20 updates)
  T=0     Book using stored captchas + latest court list
"""

import base64
import sys
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import cv2
import numpy as np

# Add parent modules to path
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "cas_http"))
sys.path.insert(0, os.path.join(_root, "slider_match"))

import run_slider_match  # noqa: E402
from cas_login import CASLogin, CASLoginError  # noqa: E402
from booking_api import (  # noqa: E402
    query_times,
    query_seats,
    fetch_captcha,
    submit_order,
    submit_booking,
    generate_yzm_data,
)

import httpx  # noqa: E402
import log_manager  # noqa: E402
from site_config import get_channel, normalize_channel  # noqa: E402

VENUES = [
    {"id": 101, "name": "一号巨构", "totalCourts": 3},
    {"id": 103, "name": "二号巨构", "totalCourts": 6},
    {"id": 104, "name": "三号巨构", "totalCourts": 3},
]


# ==================== Error Classification (matches userscript) ====================

def is_daily_limit_error(msg: str) -> bool:
    return bool(re.search(r"当天预订数量超过限制|当天.*限制|预订.*上限|当天.*上限|超过.*限制", str(msg or "")))

def is_same_slot_booked_error(msg: str) -> bool:
    return bool(re.search(r"同时段已订|同时段.*已.*订|重复预订|该时段.*已.*预订|同时段.*相同", str(msg or "")))

def is_captcha_error(msg: str) -> bool:
    return bool(re.search(r"验证码|yzm|滑块|校验|验证", str(msg or "")))

def is_retryable_error(msg: str) -> bool:
    """网络有误/数据有误/时间窗口未到 — 应重试当前场地"""
    return bool(re.search(r"网络有误|数据有误|请稍后|08:40.*21:40|再来预订|请重新预订", str(msg or "")))

def is_no_seat_error(msg: str) -> bool:
    """没有座位/已被抢 — 跳过该场地不重试"""
    return bool(re.search(r"没有座位|无座位|已被预订|已售|不可用|已被占用|库存不足|无可用", str(msg or "")))


# ==================== Per-Profile Runtime ====================

@dataclass
class ProfileRuntime:
    profile_id: int
    status: str = "idle"
    client: httpx.Client | None = None          # Authenticated session (for booking only)
    task: object = None                          # asyncio.Task
    query_task: object = None                    # continuous query task
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cached_candidates: dict = field(default_factory=dict)   # Latest court availability
    prefetched_captchas: list = field(default_factory=list)  # Pre-fetched captchas
    captcha_lock: threading.Lock = field(default_factory=threading.Lock)
    prefetch_lock: threading.Lock = field(default_factory=threading.Lock)
    continuous_query_running: bool = False
    session_ready: bool = False
    session_ready_reason: str = "not checked"
    session_ready_checked_at: float = 0.0
    channel: str = "8080"


def _format_booked_court_line(court: dict) -> str:
    return f"{court.get('timeSlot', '')} {court.get('court', {}).get('courtName', '')}".strip()


def build_latest_booking_result(booked_courts: list[dict]) -> str:
    if not booked_courts:
        return "未抢到场地"

    lines = []
    for court in booked_courts:
        line = _format_booked_court_line(court)
        if line and line not in lines:
            lines.append(line)
    return "\n".join(lines) if lines else "未抢到场地"


def get_target_booking_date(profile: dict) -> str:
    target_days = profile.get("target_days", [2])
    offset = target_days[0] if target_days else 2
    return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


def build_booking_flow_result(profile: dict, booked_courts: list[dict] | None = None,
                              persist: bool = True) -> dict:
    booked_courts = booked_courts or []
    return {
        "persist_latest_result": persist,
        "latest_booking_result": build_latest_booking_result(booked_courts) if persist else "",
        "booking_date": get_target_booking_date(profile),
    }


def _slot_start_minutes(time_slot: str) -> int | None:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-", str(time_slot or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute


def _find_matching_slot(times: list[dict], preferred_time: str) -> dict | None:
    exact = next((t for t in times if t.get("TIME_NO") == preferred_time), None)
    if exact:
        return exact

    preferred_start = _slot_start_minutes(preferred_time)
    if preferred_start is None:
        return None

    same_hour_candidates = []
    for slot in times:
        slot_start = _slot_start_minutes(slot.get("TIME_NO", ""))
        if slot_start is None:
            continue
        diff = abs(slot_start - preferred_start)
        if diff <= 45 and slot_start // 60 == preferred_start // 60:
            same_hour_candidates.append((diff, slot_start, slot))

    if not same_hour_candidates:
        return None
    same_hour_candidates.sort(key=lambda item: (item[0], item[1]))
    return same_hour_candidates[0][2]


_runtimes: dict[int, ProfileRuntime] = {}
CAPTCHA_TTL_SECONDS = 20
CAPTCHA_PREFETCH_EXTRA_ATTEMPTS = 4
CAPTCHA_PREFETCH_MAX_WORKERS = 6
QUERY_MAX_WORKERS = 12
DISPLAY_SCALE_RATIO = 260 / 590
ANON_CLIENT_POOL_SIZE = QUERY_MAX_WORKERS + CAPTCHA_PREFETCH_MAX_WORKERS
ANON_CLIENT_HEADERS = {
    "User-Agent": get_channel().user_agent,
    "X-Requested-With": "XMLHttpRequest",
}
AUTH_CHECK_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
ANON_CLIENT_LIMITS = httpx.Limits(
    max_connections=ANON_CLIENT_POOL_SIZE,
    max_keepalive_connections=ANON_CLIENT_POOL_SIZE,
)


class AnonymousClientPool:
    def __init__(self, max_clients: int):
        self.max_clients = max_clients
        self._available: list[httpx.Client] = []
        self._created = 0
        self._cond = threading.Condition()

    def acquire(self) -> httpx.Client:
        with self._cond:
            while True:
                if self._available:
                    return self._available.pop()
                if self._created < self.max_clients:
                    self._created += 1
                    break
                self._cond.wait(timeout=0.05)

        try:
            return create_anonymous_client()
        except Exception:
            with self._cond:
                self._created -= 1
                self._cond.notify()
            raise

    def release(self, client: httpx.Client):
        with self._cond:
            self._available.append(client)
            self._cond.notify()


_anon_client_pool = AnonymousClientPool(ANON_CLIENT_POOL_SIZE)


def get_runtime(profile_id: int) -> ProfileRuntime:
    if profile_id not in _runtimes:
        _runtimes[profile_id] = ProfileRuntime(profile_id=profile_id)
    return _runtimes[profile_id]


def remove_runtime(profile_id: int):
    rt = _runtimes.pop(profile_id, None)
    if rt and rt.client:
        try:
            rt.client.close()
        except Exception:
            pass


def mark_session_ready(rt: ProfileRuntime, ready: bool, reason: str):
    rt.session_ready = ready
    rt.session_ready_reason = reason
    rt.session_ready_checked_at = time.time()


def _prune_prefetched_captchas_locked(rt: ProfileRuntime, now: float | None = None) -> list:
    if now is None:
        now = time.time()
    rt.prefetched_captchas = [
        item for item in rt.prefetched_captchas
        if now - item["timestamp"] < CAPTCHA_TTL_SECONDS
    ]
    return rt.prefetched_captchas


def clear_prefetched_captchas(rt: ProfileRuntime):
    with rt.captcha_lock:
        rt.prefetched_captchas.clear()


def get_valid_prefetched_captchas(rt: ProfileRuntime, now: float | None = None) -> list:
    with rt.captcha_lock:
        return list(_prune_prefetched_captchas_locked(rt, now))


def add_prefetched_captcha(rt: ProfileRuntime, item: dict) -> int:
    with rt.captcha_lock:
        _prune_prefetched_captchas_locked(rt, item["timestamp"])
        rt.prefetched_captchas.append(item)
        return len(rt.prefetched_captchas)


def pop_valid_prefetched_captcha(rt: ProfileRuntime, now: float | None = None) -> dict | None:
    with rt.captcha_lock:
        _prune_prefetched_captchas_locked(rt, now)
        if not rt.prefetched_captchas:
            return None
        return rt.prefetched_captchas.pop(0)


def create_anonymous_client() -> httpx.Client:
    return httpx.Client(
        timeout=10,
        headers=ANON_CLIENT_HEADERS,
        trust_env=False,
        limits=ANON_CLIENT_LIMITS,
    )


@contextmanager
def lease_anonymous_client():
    client = _anon_client_pool.acquire()
    try:
        yield client
    finally:
        _anon_client_pool.release(client)


def decode_base64_image_data(data_url: str, flags: int) -> np.ndarray:
    base64_data = data_url.split(",", 1)[1] if "," in data_url else data_url
    image_data = base64.b64decode(base64_data)
    image_array = np.frombuffer(image_data, dtype=np.uint8)
    image = cv2.imdecode(image_array, flags)
    if image is None:
        raise ValueError("invalid image data")
    return image


def match_slider_local(bg_base64: str, slider_base64: str) -> dict:
    bg_img = decode_base64_image_data(bg_base64, cv2.IMREAD_COLOR)
    sd_img = decode_base64_image_data(slider_base64, cv2.IMREAD_UNCHANGED)
    top_left, score, _, _ = run_slider_match.match_slider_images(bg_img, sd_img)
    raw_x, raw_y = top_left
    return {
        "success": True,
        "rawX": int(raw_x),
        "rawY": int(raw_y),
        "displayX": int(raw_x * DISPLAY_SCALE_RATIO),
        "score": round(score, 4),
        "scaleRatio": round(DISPLAY_SCALE_RATIO, 4),
    }


def profile_channel(profile: dict | None = None, runtime: ProfileRuntime | None = None) -> str:
    if profile and profile.get("booking_channel"):
        return normalize_channel(profile.get("booking_channel"))
    if runtime and runtime.channel:
        return normalize_channel(runtime.channel)
    return "8080"


def drop_booking_session(rt: ProfileRuntime, reason: str = "session cleared"):
    if rt.client:
        try:
            rt.client.close()
        except Exception:
            pass
        rt.client = None
    mark_session_ready(rt, False, reason)


def fetch_and_match_captcha_anonymous(channel: str = "8080") -> dict:
    fetch_started = time.perf_counter()
    with lease_anonymous_client() as client:
        captcha_id, bg, slider = fetch_captcha(client, channel)
    fetch_ms = (time.perf_counter() - fetch_started) * 1000

    match_started = time.perf_counter()
    match_result = match_slider_local(bg, slider)
    match_ms = (time.perf_counter() - match_started) * 1000

    return {
        "captcha_id": captcha_id,
        "match": match_result,
        "timestamp": time.time(),
        "fetch_ms": fetch_ms,
        "match_ms": match_ms,
        "total_ms": fetch_ms + match_ms,
    }


def query_times_anonymous(venue_id: int, date: str, channel: str = "8080") -> list:
    with lease_anonymous_client() as client:
        return query_times(client, venue_id, date, channel)


def query_seats_anonymous(venue_id: int, stock_id: str, date: str = "", channel: str = "8080") -> list:
    with lease_anonymous_client() as client:
        return query_seats(client, venue_id, stock_id, date or None, channel)


def _has_booking_cookies(client: httpx.Client) -> bool:
    for cookie in client.cookies.jar:
        domain = (cookie.domain or "").lstrip(".")
        if cookie.name in ("SESSION", "JSESSIONID") and (
            "202.117.17.144" in domain or domain in ("", "localhost")
        ):
            return True
    return False


def verify_booking_session(client: httpx.Client | None, channel: str | None = None) -> tuple[bool, str]:
    if client is None:
        return False, "no client"

    cfg = get_channel(channel or getattr(client, "booking_channel", None))
    cookie_snapshot = list(client.cookies.jar)
    last_reason = "no response"
    resp = None
    for attempt in range(4):
        try:
            resp = client.get(
                cfg.orders_check_url,
                params={"page": 1, "rows": 1, "sort": "createdate", "order": "desc"},
                headers=AUTH_CHECK_HEADERS,
                timeout=8,
                follow_redirects=False,
            )
        except Exception as e:
            last_reason = f"request failed: {e}"
            time.sleep(0.4 * (attempt + 1))
            continue

        if resp.status_code in (301, 302, 303, 307, 308):
            last_reason = f"redirect {resp.status_code} -> {resp.headers.get('location', '')[:120]}"
            break
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                last_reason = "non-json body"
            else:
                if isinstance(data, dict) and ("rows" in data or "total" in data):
                    return True, f"rows={len(data.get('rows', []))}"
                last_reason = "unexpected payload"
            break
        last_reason = f"status={resp.status_code}"
        if resp.status_code < 500:
            break
        time.sleep(0.4 * (attempt + 1))

    try:
        client.cookies.jar.clear()
        for cookie in cookie_snapshot:
            client.cookies.jar.set_cookie(cookie)
    except Exception:
        pass
    return False, last_reason


# ==================== Step 1: Login (T-60s) ====================

def do_login(profile_id: int, username: str, password: str, channel: str | None = None) -> bool:
    """Login and store authenticated client. Runs in thread."""
    log = lambda msg: log_manager.emit(profile_id, msg)
    rt = get_runtime(profile_id)
    rt.channel = normalize_channel(channel or rt.channel)

    if rt.client:
        try:
            rt.client.close()
        except Exception:
            pass
        rt.client = None
    mark_session_ready(rt, False, "not logged in")

    log(f"[Login] CAS login starting on {get_channel(rt.channel).label}...")
    try:
        cas = CASLogin(logger=log, channel=rt.channel)
        client = cas.login(username, password)
        session_valid, session_reason = verify_booking_session(client, rt.channel)
        if not session_valid:
            transient = session_reason.startswith("status=5") or session_reason.startswith("request failed")
            if transient and _has_booking_cookies(client):
                log(f"[Login] CAS login accepted; orders API still {session_reason}")
            else:
                try:
                    client.close()
                except Exception:
                    pass
                rt.client = None
                mark_session_ready(rt, False, session_reason)
                log(f"[Login] CAS login returned unusable session ({session_reason})")
                return False

        rt.client = client
        mark_session_ready(rt, False, "awaiting T-2s validation")
        log(f"[Login] CAS login successful ({session_reason})")
        return True
    except CASLoginError as e:
        mark_session_ready(rt, False, str(e))
        log(f"[Login] CAS login failed: {e}")
        return False
    except Exception as e:
        mark_session_ready(rt, False, str(e))
        log(f"[Login] CAS login error: {e}")
        return False


# ==================== Step 2: Prefetch Captchas (T-6s) ====================

def do_prefetch_captchas(profile_id: int, count: int = 6) -> list:
    """Prefetch captchas anonymously in parallel. Retries on fetch/match failures."""
    log = lambda msg: log_manager.emit(profile_id, msg)
    rt = get_runtime(profile_id)

    if not rt.prefetch_lock.acquire(blocking=False):
        existing = get_valid_prefetched_captchas(rt)
        log(f"[Captcha] Prefetch already running, reusing {len(existing)} in-flight captchas")
        return existing

    try:
        existing = get_valid_prefetched_captchas(rt)
        if len(existing) >= count:
            log(f"[Captcha] Reusing {len(existing)} cached captchas")
            return existing

        needed = count - len(existing)
        worker_count = min(CAPTCHA_PREFETCH_MAX_WORKERS, needed)
        max_attempts = needed + CAPTCHA_PREFETCH_EXTRA_ATTEMPTS
        log(
            f"[Captcha] Prefetching {needed} captchas anonymously in parallel "
            f"(workers={worker_count}, have {len(existing)}, target {count})..."
        )
        t0 = time.time()
        attempts = 0

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures: dict = {}

            def submit_attempt():
                nonlocal attempts
                attempts += 1
                futures[executor.submit(fetch_and_match_captcha_anonymous, rt.channel)] = attempts

            for _ in range(min(worker_count, max_attempts)):
                submit_attempt()

            while futures and not rt.cancel_event.is_set():
                done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    attempt_no = futures.pop(future)
                    try:
                        item = future.result()
                        available = add_prefetched_captcha(rt, item)
                        match_result = item["match"]
                        log(
                            f"[Captcha]   #{available}: displayX={match_result['displayX']} "
                            f"score={match_result.get('score', 0):.3f} "
                            f"(fetch:{item['fetch_ms']:.0f}ms match:{item['match_ms']:.0f}ms total:{item['total_ms']:.0f}ms)"
                        )
                    except Exception as e:
                        log(f"[Captcha]   attempt #{attempt_no} failed: {str(e)[:80]}")
                        if attempts < max_attempts and len(get_valid_prefetched_captchas(rt)) < count and not rt.cancel_event.is_set():
                            submit_attempt()

                if len(get_valid_prefetched_captchas(rt)) >= count:
                    break

        final_results = get_valid_prefetched_captchas(rt)
        elapsed = (time.time() - t0) * 1000
        log(f"[Captcha] Prefetch done: {min(len(final_results), count)}/{count} OK ({elapsed:.0f}ms)")
        return final_results
    finally:
        rt.prefetch_lock.release()


# ==================== Step 3: Query Courts (Anonymous, T-Xms) ====================

def do_query_candidates(profile_id: int, profile: dict) -> dict:
    """
    Query available courts using ANONYMOUS clients in parallel.
    This avoids session contention with the booking requests.
    Returns {timeSlot: [candidates sorted by priority]}
    """
    log = lambda msg: log_manager.emit(profile_id, msg)

    venue_prefs = [v for v in profile.get("venue_prefs", []) if v.get("enabled")]
    time_prefs = [t for t in profile.get("time_prefs", []) if t.get("enabled") and t.get("fanout", 0) > 0]
    date = get_target_booking_date(profile)
    rt = get_runtime(profile_id)
    channel = profile_channel(profile, rt)
    rt.channel = channel

    if not venue_prefs or not time_prefs:
        return {}

    results = {tp["time"]: [] for tp in time_prefs}
    total_started = time.perf_counter()

    stage1_started = time.perf_counter()
    venue_times_map: dict[int, list] = {}
    venue_workers = min(QUERY_MAX_WORKERS, len(venue_prefs))
    with ThreadPoolExecutor(max_workers=venue_workers) as executor:
        future_to_venue = {
            executor.submit(query_times_anonymous, vp["id"], date, channel): vp
            for vp in venue_prefs
        }
        for future in future_to_venue:
            vp = future_to_venue[future]
            try:
                venue_times_map[vp["id"]] = future.result()
            except Exception as e:
                venue_times_map[vp["id"]] = []
                log(f"[Query] {vp['name']} times failed: {e}")

    seat_jobs = []
    for tp in time_prefs:
        for vp in venue_prefs:
            times = venue_times_map.get(vp["id"], [])
            slot = _find_matching_slot(times, tp["time"])
            if slot:
                seat_jobs.append((tp, vp, slot))

    stage2_started = time.perf_counter()
    if seat_jobs:
        seat_workers = min(QUERY_MAX_WORKERS, len(seat_jobs))
        with ThreadPoolExecutor(max_workers=seat_workers) as executor:
            future_to_job = {
                executor.submit(query_seats_anonymous, vp["id"], slot["ID"], date, channel): (tp, vp, slot)
                for tp, vp, slot in seat_jobs
            }
            for future in future_to_job:
                tp, vp, slot = future_to_job[future]
                try:
                    seats = future.result()
                except Exception as e:
                    log(f"[Query] {vp['name']} seats failed: {e}")
                    continue

                allowed = set(vp.get("courts", []))
                court_priority = vp.get("courtPriority", {})
                for seat in seats:
                    if not seat["available"]:
                        continue
                    if allowed and seat["courtNumber"] not in allowed:
                        continue
                    pri = court_priority.get(str(seat["courtNumber"]), seat["courtNumber"])
                    results[tp["time"]].append({
                        "venueId": vp["id"],
                        "venueName": vp["name"],
                        "date": date,
                        "timeSlot": slot.get("TIME_NO", tp["time"]),
                        "stockId": slot["ID"],
                        "court": {
                            "courtNumber": seat["courtNumber"],
                            "courtName": f"场地{seat['courtNumber']}",
                            "seatId": seat["seatId"],
                        },
                        "priority": pri,
                    })

    for time_no in results:
        results[time_no].sort(key=lambda c: c["priority"])

    total = sum(len(v) for v in results.values())
    active_slots = [k for k, v in results.items() if v]
    total_ms = (time.perf_counter() - total_started) * 1000
    stage1_ms = (stage2_started - stage1_started) * 1000
    stage2_ms = (time.perf_counter() - stage2_started) * 1000
    log(
        f"[Query] {date}: {total} candidates in {len(active_slots)} slots "
        f"(times:{stage1_ms:.0f}ms seats:{stage2_ms:.0f}ms total:{total_ms:.0f}ms)"
    )
    for slot_time in active_slots:
        courts = results[slot_time]
        court_list = ", ".join(
            f"{c['venueName']} {c['court']['courtName']}(P{c['priority']})"
            for c in courts
        )
        log(f"[Query]   {slot_time}: {court_list}")
    return results


# ==================== Step 4: Book Single Court ====================

SLIDER_MAX_RETRIES = 2


def do_book_court(profile_id: int, court: dict) -> dict:
    """
    Book a single court. Only retries on captcha errors.
    Business errors (same_slot_booked, daily_limit) return immediately.
    """
    log = lambda msg: log_manager.emit(profile_id, msg)
    rt = get_runtime(profile_id)
    client = rt.client
    tag = f"[{court['timeSlot']}][{court['court']['courtName']}]"
    use_precomputed = True

    for attempt in range(1, SLIDER_MAX_RETRIES + 1):
        if rt.cancel_event.is_set():
            return {"success": False, "message": "Cancelled"}

        t_start = time.time()
        try:
            # Step A: Submit order page (writes to server session)
            log(f"{tag} -> order...")
            submit_order(client, court["venueId"], court["stockId"], court["court"]["seatId"], rt.channel)

            # Step B: Get captcha (use prefetched if available)
            precomputed = None
            if use_precomputed:
                precomputed = pop_valid_prefetched_captcha(rt)

            if precomputed:
                captcha_id = precomputed["captcha_id"]
                display_x = precomputed["match"]["displayX"]
                log(f"{tag} <- pre-captcha (x={display_x})")
            else:
                log(f"{tag} -> captcha...")
                captcha = fetch_and_match_captcha_anonymous(rt.channel)
                captcha_id = captcha["captcha_id"]
                match_result = captcha["match"]
                display_x = match_result["displayX"]
                log(
                    f"{tag} <- captcha x={display_x} score={match_result.get('score', 0):.3f} "
                    f"(fetch:{captcha['fetch_ms']:.0f}ms match:{captcha['match_ms']:.0f}ms)"
                )

            # Step C: Generate track + submit booking
            yzm_data = generate_yzm_data(display_x, captcha_id, rt.channel)
            log(f"{tag} -> submit...")
            result = submit_booking(client, court["venueId"], court["stockId"],
                                    court["court"]["seatId"], yzm_data, rt.channel)

            elapsed = (time.time() - t_start) * 1000
            success = result.get("result") == "1" or result.get("success")
            msg = result.get("message") or result.get("msg") or str(result)

            if success:
                log(f"{tag} BOOKED! ({elapsed:.0f}ms) {msg}")
                return {"success": True, "message": msg, "elapsed": elapsed}

            log(f"{tag} Failed: {msg} ({elapsed:.0f}ms)")

            # --- Error classification & retry logic ---

            # 1. Daily limit: stop everything immediately
            if is_daily_limit_error(msg):
                return {"success": False, "message": msg, "daily_limit": True}

            # 2. Same slot booked: stop this slot immediately
            if is_same_slot_booked_error(msg):
                return {"success": False, "message": msg, "same_slot_booked": True}

            # 3. No seat (grabbed by others): skip this court, no retry
            if is_no_seat_error(msg):
                log(f"{tag} Court taken, skip")
                return {"success": False, "message": msg}

            # 4. Captcha error: retry with realtime captcha
            if is_captcha_error(msg):
                if precomputed:
                    log(f"{tag} Pre-captcha invalid, switching to realtime...")
                    use_precomputed = False
                if attempt < SLIDER_MAX_RETRIES:
                    continue
                return {"success": False, "message": msg}

            # 5. Retryable (network error / data error / time window not open): retry once
            if is_retryable_error(msg):
                if attempt < SLIDER_MAX_RETRIES:
                    log(f"{tag} Retryable error, retrying...")
                    time.sleep(0.5)  # Brief pause before retry
                    continue
                return {"success": False, "message": msg}

            # 6. Unknown error: don't retry, let caller try next court
            return {"success": False, "message": msg}

        except Exception as e:
            elapsed = (time.time() - t_start) * 1000
            log(f"{tag} Error: {e} ({elapsed:.0f}ms)")
            # Network/timeout exceptions are retryable
            if attempt < SLIDER_MAX_RETRIES:
                log(f"{tag} Retrying...")
                continue
            return {"success": False, "message": str(e)}

    return {"success": False, "message": "Retries exhausted"}


# ==================== Step 5: Book Time Slot ====================

def _book_time_slot(profile_id: int, time_slot: str, fanout: int,
                    parallel: int, shared: dict) -> dict:
    """
    Book one time slot by trying courts in priority order.
    Keeps up to `parallel` court attempts in flight and backfills with the
    next highest-priority candidate when a previous attempt fails.
    Reads LATEST court list from rt.cached_candidates each attempt.
    """
    log = lambda msg: log_manager.emit(profile_id, msg)
    rt = get_runtime(profile_id)
    max_parallel = max(1, min(int(parallel or 1), fanout or 1))
    tried_courts = set()
    slot_state = {
        "tried": 0,
        "success": False,
        "same_slot_booked": False,
    }
    slot_lock = threading.Lock()

    if max_parallel > 1:
        log(f"[{time_slot}] Slot parallel={max_parallel}, fanout={fanout}")

    def _take_next_candidate():
        with slot_lock:
            if slot_state["success"] or slot_state["same_slot_booked"] or slot_state["tried"] >= fanout:
                return None, None

            slot_candidates = rt.cached_candidates.get(time_slot, [])
            for candidate in slot_candidates:
                court_key = f"{candidate['venueId']}_{candidate['court']['courtNumber']}"
                if court_key in tried_courts:
                    continue
                tried_courts.add(court_key)
                slot_state["tried"] += 1
                return slot_state["tried"], candidate
        return None, None

    def _worker():
        while True:
            with shared["lock"]:
                if shared["successful"] >= shared["max_bookings"]:
                    return {"success": False}
                if shared["daily_limit_reached"]:
                    return {"success": False, "daily_limit": True}
            if rt.cancel_event.is_set():
                return {"success": False}

            attempt_no, court = _take_next_candidate()
            if not court:
                return {"success": False}

            log(f"[{time_slot}] Try #{attempt_no}/{fanout}: {court['venueName']} {court['court']['courtName']}")
            result = do_book_court(profile_id, court)

            if result["success"]:
                with slot_lock:
                    if slot_state["success"]:
                        return {"success": False}
                    slot_state["success"] = True
                with shared["lock"]:
                    shared["successful"] += 1
                    shared["booked_courts"].append(court)
                    count = shared["successful"]
                log(f"[{time_slot}] Booked! ({count}/{shared['max_bookings']})")
                return {"success": True, "court": court}

            msg = result.get("message", "")

            if result.get("daily_limit") or is_daily_limit_error(msg):
                with shared["lock"]:
                    shared["daily_limit_reached"] = True
                log(f"[{time_slot}] Daily limit reached, stopping all")
                return {"success": False, "daily_limit": True}

            if result.get("same_slot_booked") or is_same_slot_booked_error(msg):
                with slot_lock:
                    slot_state["same_slot_booked"] = True
                log(f"[{time_slot}] Same slot already booked, skip this slot")
                return {"success": False, "same_slot_booked": True}

            log(f"[{time_slot}] Failed: {msg}, trying next court...")

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = [executor.submit(_worker) for _ in range(max_parallel)]
        results = [future.result() for future in futures]

    if any(r.get("success") for r in results):
        for r in results:
            if r.get("success"):
                return r
    if any(r.get("daily_limit") for r in results):
        return {"success": False, "daily_limit": True}
    if any(r.get("same_slot_booked") for r in results):
        return {"success": False, "same_slot_booked": True}
    if slot_state["tried"] >= fanout or tried_courts:
        log(f"[{time_slot}] No more candidates")
    return {"success": False}


# ==================== Full Booking Flow ====================

def run_booking_flow(profile_id: int, profile: dict):
    """
    Execute booking at T=0. Expects:
    - rt.client already set (from T-60s login)
    - rt.prefetched_captchas already filled (from T-6s prefetch)
    - rt.cached_candidates already filled (from T-Xms continuous query)
    Falls back to doing these steps if not already done.
    """
    # Start a new log session for this booking run
    log_manager.start_log_session(profile_id)
    log = lambda msg: log_manager.emit(profile_id, msg)
    def set_status(s):
        rt.status = s
        log_manager.emit(profile_id, f"Status: {s}", status=s)
    rt = get_runtime(profile_id)
    rt.channel = profile_channel(profile, rt)
    set_status("running")
    rt.cancel_event.clear()

    time_prefs = [t for t in profile.get("time_prefs", []) if t.get("enabled") and t.get("fanout", 0) > 0]
    max_bookings = profile.get("max_bookings", 2)

    shared = {
        "successful": 0,
        "max_bookings": max_bookings,
        "daily_limit_reached": False,
        "booked_courts": [],
        "lock": threading.Lock(),
    }

    try:
        # Require a valid session prechecked at T-2s. T=0 does not re-login or re-check.
        if not rt.client:
            log("[T=0] No valid session prepared before booking, aborting booking")
            set_status("failed")
            return build_booking_flow_result(profile, [])

        if not rt.session_ready_checked_at:
            log("[T=0] Final session check missing, aborting booking")
            set_status("failed")
            return build_booking_flow_result(profile, [])

        if rt.session_ready:
            log(f"[T=0] Session precheck OK ({rt.session_ready_reason})")
        else:
            log(f"[T=0] Session precheck failed ({rt.session_ready_reason}), aborting booking")
            set_status("failed")
            return build_booking_flow_result(profile, [])

        if rt.cancel_event.is_set():
            set_status("idle")
            return build_booking_flow_result(profile, persist=False)

        # Fallback: Query if not already done by continuous query
        if not rt.cached_candidates or not any(rt.cached_candidates.values()):
            log("[T=0] No cached candidates (continuous query missed), querying now...")
            rt.cached_candidates = do_query_candidates(profile_id, profile)
            if not any(rt.cached_candidates.values()):
                log("[T=0] No available candidates found")
                set_status("failed")
                return build_booking_flow_result(profile, [])

        total = sum(len(v) for v in rt.cached_candidates.values())
        log(f"[T=0] Using {total} cached candidates")

        if rt.cancel_event.is_set():
            set_status("idle")
            return build_booking_flow_result(profile, persist=False)

        # Fallback: Prefetch captchas if not already done at T-6s
        valid_captchas = get_valid_prefetched_captchas(rt)
        if valid_captchas:
            log(f"[T=0] Using {len(valid_captchas)} pre-fetched captchas")
        elif rt.prefetch_lock.locked():
            log("[T=0] Captcha prefetch still running, skipping duplicate batch fetch")
        else:
            log("[T=0] No valid pre-fetched captchas, fetching 1 fallback captcha...")
            fallback = do_prefetch_captchas(profile_id, count=1)
            if fallback:
                log(f"[T=0] Fallback captcha ready ({len(fallback)} available)")
            else:
                log("[T=0] Fallback captcha unavailable, booking will fetch captcha inline")

        if rt.cancel_event.is_set():
            set_status("idle")
            return

        # Wait for booking time window (08:40-21:40) if needed
        now_dt = datetime.now()
        cur_minutes = now_dt.hour * 60 + now_dt.minute
        if cur_minutes < 8 * 60 + 40:
            wait_seconds = (8 * 60 + 40 - cur_minutes) * 60 - now_dt.second
            if 0 < wait_seconds < 15:
                log(f"[T=0] Waiting {wait_seconds}s for booking window (08:40)...")
                for _ in range(wait_seconds * 10):
                    if rt.cancel_event.is_set():
                        set_status("idle")
                        return build_booking_flow_result(profile, persist=False)
                    time.sleep(0.1)

        # ---- START BOOKING ----
        log("--- Start booking ---")
        t_start = time.time()

        # Group time slots by priority
        priority_groups = {}
        for tp in time_prefs:
            pri = tp.get("priority", 1)
            priority_groups.setdefault(pri, []).append(tp)
        sorted_priorities = sorted(priority_groups.keys())

        log(f"Priority groups: " + " -> ".join(
            f"P{p}({','.join(t['time'] for t in priority_groups[p])})"
            for p in sorted_priorities))

        for pri in sorted_priorities:
            if shared["successful"] >= max_bookings or shared["daily_limit_reached"]:
                break
            if rt.cancel_event.is_set():
                break

            group = priority_groups[pri]
            log(f"--- Priority {pri}: {', '.join(t['time'] for t in group)} ---")

            if len(group) == 1:
                tp = group[0]
                _book_time_slot(
                    profile_id,
                    tp["time"],
                    tp.get("fanout", 4),
                    tp.get("parallel", 1),
                    shared,
                )
            else:
                # Same priority = concurrent execution (matches Promise.all)
                with ThreadPoolExecutor(max_workers=len(group)) as executor:
                    futures = []
                    for tp in group:
                        f = executor.submit(
                            _book_time_slot,
                            profile_id,
                            tp["time"],
                            tp.get("fanout", 4),
                            tp.get("parallel", 1),
                            shared,
                        )
                        futures.append(f)
                    results = [f.result() for f in futures]
                    if any(r.get("daily_limit") for r in results):
                        shared["daily_limit_reached"] = True
                        break

        # Stop continuous query
        rt.continuous_query_running = False

        elapsed = (time.time() - t_start) * 1000
        log(f"--- Booking done: {shared['successful']} booked, {elapsed:.0f}ms ---")

        if shared["successful"] > 0:
            set_status("success")
        else:
            set_status("failed")
        return build_booking_flow_result(profile, shared["booked_courts"])

    except Exception as e:
        log(f"Booking flow error: {e}")
        set_status("failed")
        return build_booking_flow_result(profile, [])


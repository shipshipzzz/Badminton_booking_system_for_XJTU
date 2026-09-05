"""
Core booking logic: wraps cas_login + booking_api for per-profile execution.

Timeline:
  T-12h   Fill the candidate pool (selected courts -> server stock/seat IDs)
  T-150s  Login check, repeated every 60s (T-90s, T-30s); re-login if invalid
  T-6s    Prefetch captchas (store for use at T=0)
  T-Xms   Live court query (anonymous, every 1s, max 20 updates) refines the pool
  T=0     Book from the candidate pool immediately; no blocking query
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
    fetch_ok_area,
    submit_order,
    submit_booking,
    generate_yzm_data,
    _times_from_ok_area,
    seats_from_ok_area,
)

import httpx  # noqa: E402
import log_manager  # noqa: E402
from site_config import get_channel, normalize_channel  # noqa: E402
from http_capture import capture_context, submit_with_capture_context
from dual_slot import (  # noqa: E402
    collect_priority_levels,
    collect_query_slot_times,
    dual_occupied_times,
    filter_single_time_prefs,
    get_dual_slot_prefs,
    pick_dual_pairs,
    tasks_for_wave,
)
from candidate_pool import (  # noqa: E402
    CandidatePool,
    bookable_entries,
    bookable_signature,
    describe_slots,
    find_matching_slot as _find_matching_slot,
    merge_venue_results,
    pool_summary,
    rebuild_pool,
)

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
    pool: CandidatePool | None = None            # Candidate pool for the target date
    pool_log_signature: tuple = ()               # Last logged bookable composition
    target_at: datetime | None = None            # Scheduled T (None when not scheduled)
    prefetched_captchas: list = field(default_factory=list)  # Pre-fetched captchas
    captcha_lock: threading.Lock = field(default_factory=threading.Lock)
    prefetch_lock: threading.Lock = field(default_factory=threading.Lock)
    book_lock: threading.Lock = field(default_factory=threading.Lock)
    candidates_lock: threading.Lock = field(default_factory=threading.Lock)
    login_lock: threading.Lock = field(default_factory=threading.Lock)
    query_seq: int = 0
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


def _target_day_offset(profile: dict) -> int:
    target_days = profile.get("target_days") or [2]
    try:
        return int(target_days[0])
    except (TypeError, ValueError, IndexError):
        return 2


def get_target_booking_date(profile: dict, base: datetime | None = None) -> str:
    """Booking date = base day + target offset. Base defaults to now."""
    base = base or datetime.now()
    return (base + timedelta(days=_target_day_offset(profile))).strftime("%Y-%m-%d")


def target_booking_date(profile: dict, rt: "ProfileRuntime | None" = None) -> str:
    """Booking date relative to the scheduled T when one is pending, else to now."""
    base = rt.target_at if rt is not None and rt.target_at else None
    return get_target_booking_date(profile, base)


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


def ensure_pool(rt: ProfileRuntime, profile: dict, date: str) -> CandidatePool:
    """Make sure rt.pool covers `date` and the current selection. Keeps known IDs."""
    slot_times = collect_query_slot_times(profile)
    with rt.candidates_lock:
        rt.pool = rebuild_pool(rt.pool, profile, date, slot_times)
        return rt.pool


def rebuild_pool_for_profile(rt: ProfileRuntime, profile: dict, date: str) -> dict | None:
    """Rebuild the default pool after a selection change. Returns the summary."""
    ensure_pool(rt, profile, date)
    with rt.candidates_lock:
        return pool_summary(rt.pool)


def pool_status(profile_id: int) -> dict | None:
    rt = _runtimes.get(profile_id)
    if rt is None:
        return None
    with rt.candidates_lock:
        return pool_summary(rt.pool)


def begin_query_seq(rt: ProfileRuntime) -> tuple[int, float]:
    with rt.candidates_lock:
        rt.query_seq += 1
        return rt.query_seq, time.time()


def copy_slot_candidates(rt: ProfileRuntime, *slot_times: str) -> list[list]:
    """Bookable candidates (IDs known, explicitly available) per slot, priority order."""
    with rt.candidates_lock:
        return [bookable_entries(rt.pool, time_str) for time_str in slot_times]


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


def query_ok_area_anonymous(venue_id: int, date: str, channel: str = "8080") -> list:
    with lease_anonymous_client() as client:
        return fetch_ok_area(client, venue_id, date, channel)


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
        mark_session_ready(rt, session_valid, f"login ok ({session_reason})")
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

def _alternate_channel(channel: str) -> str:
    return "80" if normalize_channel(channel) == "8080" else "8080"


def _fetch_venue_inventory(venue_prefs: list[dict], slot_times: list[str], date: str,
                           channel: str, log) -> tuple[dict[int, dict], dict]:
    """Fetch inventory on the profile's channel, then retry failed venues on the other one.

    Stock/seat IDs are identical on both channels, and the :8080 H5 channel rejects
    anonymous queries outside 08:40-21:40 while the :80 portal answers all day.
    """
    results, timing = _fetch_venue_inventory_on(venue_prefs, slot_times, date, channel, log)
    failed = [vp for vp in venue_prefs if not results.get(vp["id"], {}).get("ok")]
    if not failed:
        return results, timing

    alt = _alternate_channel(channel)
    alt_results, alt_timing = _fetch_venue_inventory_on(failed, slot_times, date, alt, log)
    recovered = []
    for vp in failed:
        if alt_results.get(vp["id"], {}).get("ok"):
            results[vp["id"]] = alt_results[vp["id"]]
            recovered.append(vp["name"])
    timing["ok"] = sum(1 for r in results.values() if r.get("ok"))
    timing["failed"] = len(results) - timing["ok"]
    for key in ("times_ms", "seats_ms", "total_ms"):
        timing[key] += alt_timing[key]
    timing["fallback"] = f"{len(recovered)}/{len(failed)} via {alt}"
    log(
        f"[Query] {', '.join(vp['name'] for vp in failed)} failed on {channel}, "
        f"retried on {alt}: recovered {', '.join(recovered) or 'none'}"
    )
    return results, timing


def _fetch_venue_inventory_on(venue_prefs: list[dict], slot_times: list[str], date: str,
                              channel: str, log) -> tuple[dict[int, dict], dict]:
    """Fetch times + seats for every venue on one channel with anonymous clients.

    Returns ({venue_id: {"ok", "times", "seats_by_stock"}}, timing). A venue whose
    times fetch failed is reported ok=False; a slot whose seat fetch failed is
    reported as seats_by_stock[stock_id] = None so the pool keeps its last state.
    """
    seat_mode = get_channel(channel).seat_mode
    results: dict[int, dict] = {}
    timing = {"ok": 0, "failed": 0}
    total_started = time.perf_counter()

    stage1_started = time.perf_counter()
    venue_rows_map: dict[int, list] = {}
    venue_workers = min(QUERY_MAX_WORKERS, max(1, len(venue_prefs)))
    with ThreadPoolExecutor(max_workers=venue_workers) as executor:
        if seat_mode == "ok_area":
            future_to_venue = {
                submit_with_capture_context(executor, query_ok_area_anonymous, vp["id"], date, channel): vp
                for vp in venue_prefs
            }
            for future in future_to_venue:
                vp = future_to_venue[future]
                try:
                    rows = future.result()
                except Exception as e:
                    rows = None
                    log(f"[Query] {vp['name']} times failed: {e}")
                if rows is None:
                    timing["failed"] += 1
                    results[vp["id"]] = {"ok": False}
                    continue
                timing["ok"] += 1
                venue_rows_map[vp["id"]] = rows
                results[vp["id"]] = {"ok": True, "times": _times_from_ok_area(rows), "seats_by_stock": {}}
        else:
            future_to_venue = {
                submit_with_capture_context(executor, query_times_anonymous, vp["id"], date, channel): vp
                for vp in venue_prefs
            }
            for future in future_to_venue:
                vp = future_to_venue[future]
                try:
                    times = future.result()
                    timing["ok"] += 1
                    results[vp["id"]] = {"ok": True, "times": times, "seats_by_stock": {}}
                except Exception as e:
                    timing["failed"] += 1
                    results[vp["id"]] = {"ok": False}
                    log(f"[Query] {vp['name']} times failed: {e}")

    seat_jobs = []
    for vp in venue_prefs:
        result = results.get(vp["id"])
        if not result or not result["ok"]:
            continue
        for time_str in slot_times:
            slot = _find_matching_slot(result["times"], time_str)
            if slot:
                seat_jobs.append((vp, slot))

    stage2_started = time.perf_counter()
    if seat_jobs:
        if seat_mode == "ok_area":
            for vp, slot in seat_jobs:
                results[vp["id"]]["seats_by_stock"][str(slot["ID"])] = seats_from_ok_area(
                    venue_rows_map.get(vp["id"], []), slot["ID"]
                )
        else:
            seat_workers = min(QUERY_MAX_WORKERS, len(seat_jobs))
            with ThreadPoolExecutor(max_workers=seat_workers) as executor:
                future_to_job = {
                    submit_with_capture_context(executor, query_seats_anonymous, vp["id"], slot["ID"], date, channel): (vp, slot)
                    for vp, slot in seat_jobs
                }
                for future in future_to_job:
                    vp, slot = future_to_job[future]
                    try:
                        seats = future.result()
                    except Exception as e:
                        log(f"[Query] {vp['name']} seats failed: {e}")
                        seats = None
                    results[vp["id"]]["seats_by_stock"][str(slot["ID"])] = seats

    finished = time.perf_counter()
    timing["times_ms"] = (stage2_started - stage1_started) * 1000
    timing["seats_ms"] = (finished - stage2_started) * 1000
    timing["total_ms"] = (finished - total_started) * 1000
    return results, timing


def do_query_candidates(profile_id: int, profile: dict, seq: int | None = None,
                        started_at: float | None = None, source: str = "live",
                        date: str | None = None) -> dict:
    """
    Query the target date's inventory with ANONYMOUS clients and merge it into
    the candidate pool. A venue that fails keeps its last known state; results
    are applied per venue by request-start time so a slow earlier query cannot
    overwrite a newer one. Returns the merge stats (empty dict when nothing ran).
    """
    log = lambda msg: log_manager.emit(profile_id, msg)

    rt = get_runtime(profile_id)
    channel = profile_channel(profile, rt)
    rt.channel = channel
    if date is None:
        date = target_booking_date(profile, rt if source != "manual" else None)

    # Pool first, then the start stamp: a request stamped before the pool cutoff is stale.
    pool = ensure_pool(rt, profile, date)
    if seq is None or started_at is None:
        seq, started_at = begin_query_seq(rt)
    venue_prefs = [v for v in profile.get("venue_prefs", []) if v.get("enabled")]
    slot_times = pool.slot_times
    tag = f"[Query] #{seq} ({source}) {date}"

    if not venue_prefs or not slot_times:
        log(f"{tag}: nothing selected (venues={len(venue_prefs)}, slots={len(slot_times)})")
        return {}

    with capture_context(profile_id=profile_id, query_seq=seq, source=source, booking_date=date):
        venue_results, timing = _fetch_venue_inventory(venue_prefs, slot_times, date, channel, log)
    timing_text = (
        f"times:{timing['times_ms']:.0f}ms seats:{timing['seats_ms']:.0f}ms total:{timing['total_ms']:.0f}ms"
    )

    with rt.candidates_lock:
        if rt.pool is None or rt.pool.date != date:
            log(f"{tag}: pool now targets {rt.pool.date if rt.pool else 'nothing'}, result discarded ({timing_text})")
            return {}
        summary_before = pool_summary(rt.pool)
        if timing["ok"] == 0:
            log(
                f"{tag}: all venues failed ({timing_text}), keeping pool "
                f"({summary_before['bookable']} bookable, last ok {summary_before['last_success_at'] or 'never'})"
            )
            return {"applied": [], "failed": list(venue_results)}
        stats = merge_venue_results(rt.pool, venue_results, started_at, seq=seq, source=source,
                                    venue_prefs=venue_prefs)
        summary = pool_summary(rt.pool)
        signature = bookable_signature(rt.pool)
        composition_changed = signature != rt.pool_log_signature
        rt.pool_log_signature = signature
        slot_lines = describe_slots(rt.pool)

    names = {vp["id"]: vp["name"] for vp in venue_prefs}
    parts = [f"venues ok={timing['ok']} failed={timing['failed']}"]
    if timing.get("fallback"):
        parts.append(f"fallback {timing['fallback']}")
    if stats["stale"]:
        parts.append("stale-discarded: " + ", ".join(names.get(v, str(v)) for v in stats["stale"]))
    log(f"{tag}: {' | '.join(parts)} ({timing_text})")
    log(
        f"[Pool] {date}: {summary['bookable']} bookable / {summary['selected']} selected "
        f"in {sum(1 for n in summary['slots'].values() if n)} slots, "
        f"{summary['unresolved']} unresolved, {summary['unavailable']} unavailable"
        + (f" | +{stats['resolved']} resolved" if stats["resolved"] else "")
    )
    if stats["removed"]:
        log("[Pool] removed (not bookable): " + ", ".join(stats["removed"]))
    if stats["restored"]:
        log("[Pool] back to bookable: " + ", ".join(stats["restored"]))
    if stats["added"]:
        log("[Pool] added: " + ", ".join(stats["added"]))
    if source != "live" or composition_changed:
        for line in slot_lines:
            log(f"[Pool]   {line}")
        if not slot_lines:
            log("[Pool]   (no bookable court yet)")
    return stats


# ==================== Step 4: Book Single Court ====================

SLIDER_MAX_RETRIES = 2


def do_book_court(profile_id: int, court: dict, shared: dict | None = None) -> dict:
    return do_book_courts(profile_id, [court], shared=shared)


def do_book_courts(profile_id: int, courts: list[dict], shared: dict | None = None) -> dict:
    """
    Book 1-2 courts in one order. One captcha, one show.html, one book.html.
    Only retries on captcha / retryable errors.
    """
    log = lambda msg: log_manager.emit(profile_id, msg)
    rt = get_runtime(profile_id)
    client = rt.client
    if not client:
        return {"success": False, "message": "No session"}
    if not courts:
        return {"success": False, "message": "No courts"}

    venue_ids = {court["venueId"] for court in courts}
    if len(venue_ids) != 1:
        return {"success": False, "message": "Must be same venue"}

    venue_id = courts[0]["venueId"]
    items = [{"stock_id": court["stockId"], "seat_id": court["court"]["seatId"]} for court in courts]
    tag = "+".join(f"[{court['timeSlot']}][{court['court']['courtName']}]" for court in courts)
    use_precomputed = True

    for attempt in range(1, SLIDER_MAX_RETRIES + 1):
        if rt.cancel_event.is_set():
            return {"success": False, "message": "Cancelled"}

        t_start = time.time()
        try:
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

            yzm_data = generate_yzm_data(display_x, captcha_id, rt.channel)
            with rt.book_lock, capture_context(profile_id=profile_id, phase="booking", attempt=attempt):
                if shared:
                    with shared["lock"]:
                        if shared["successful"] >= shared["max_bookings"] or shared["daily_limit_reached"]:
                            return {"success": False, "message": "Quota reached"}
                if rt.cancel_event.is_set():
                    return {"success": False, "message": "Cancelled"}
                log(f"{tag} -> order...")
                submit_order(client, venue_id, channel=rt.channel, items=items)
                log(f"{tag} -> submit...")
                result = submit_booking(client, venue_id, yzm_data=yzm_data, channel=rt.channel, items=items)

            elapsed = (time.time() - t_start) * 1000
            success = result.get("result") == "1" or result.get("success")
            msg = result.get("message") or result.get("msg") or str(result)

            if success:
                log(f"{tag} BOOKED! ({elapsed:.0f}ms) {msg}")
                return {"success": True, "message": msg, "elapsed": elapsed, "courts": courts}

            log(f"{tag} Failed: {msg} ({elapsed:.0f}ms)")

            if is_daily_limit_error(msg):
                return {"success": False, "message": msg, "daily_limit": True}

            if is_same_slot_booked_error(msg):
                return {"success": False, "message": msg, "same_slot_booked": True}

            if is_no_seat_error(msg):
                log(f"{tag} Court taken, skip")
                return {"success": False, "message": msg}

            if is_captcha_error(msg):
                if precomputed:
                    log(f"{tag} Pre-captcha invalid, switching to realtime...")
                    use_precomputed = False
                if attempt < SLIDER_MAX_RETRIES:
                    continue
                return {"success": False, "message": msg}

            if is_retryable_error(msg):
                if attempt < SLIDER_MAX_RETRIES:
                    log(f"{tag} Retryable error, retrying...")
                    time.sleep(0.5)
                    continue
                return {"success": False, "message": msg}

            return {"success": False, "message": msg}

        except Exception as e:
            elapsed = (time.time() - t_start) * 1000
            log(f"{tag} Error: {e} ({elapsed:.0f}ms)")
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
    Reads the LATEST candidate pool each attempt.
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

            slot_candidates = copy_slot_candidates(rt, time_slot)[0]
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
            result = do_book_court(profile_id, court, shared=shared)

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


def _book_dual_slot(profile_id: int, dual_pref: dict, shared: dict) -> dict:
    """Book one dual-slot pair (same court number across two consecutive slots)."""
    log = lambda msg: log_manager.emit(profile_id, msg)
    rt = get_runtime(profile_id)
    start_time = dual_pref["time"]
    next_time = dual_pref["next_time"]
    fanout = dual_pref.get("fanout", 4)
    parallel = dual_pref.get("parallel", 1)
    max_parallel = max(1, min(int(parallel or 1), fanout or 1))
    label = f"{start_time}+{next_time}"
    tried_courts = set()
    slot_state = {
        "tried": 0,
        "success": False,
        "same_slot_booked": False,
    }
    slot_lock = threading.Lock()

    if max_parallel > 1:
        log(f"[Dual {label}] Slot parallel={max_parallel}, fanout={fanout}")

    def _take_next_pair():
        with slot_lock:
            if slot_state["success"] or slot_state["same_slot_booked"] or slot_state["tried"] >= fanout:
                return None, None
            first_slot, second_slot = copy_slot_candidates(rt, start_time, next_time)
            pairs = pick_dual_pairs(first_slot, second_slot)
            for first, second in pairs:
                court_key = f"{first['venueId']}_{first['court']['courtNumber']}"
                if court_key in tried_courts:
                    continue
                tried_courts.add(court_key)
                slot_state["tried"] += 1
                return slot_state["tried"], (first, second)
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

            attempt_no, pair = _take_next_pair()
            if not pair:
                return {"success": False}

            first, second = pair
            log(
                f"[Dual {label}] Try #{attempt_no}/{fanout}: "
                f"{first['venueName']} {first['court']['courtName']}"
            )
            result = do_book_courts(profile_id, [first, second], shared=shared)

            if result["success"]:
                with slot_lock:
                    if slot_state["success"]:
                        return {"success": False}
                    slot_state["success"] = True
                with shared["lock"]:
                    shared["successful"] += 2
                    shared["booked_courts"].extend([first, second])
                    count = shared["successful"]
                log(f"[Dual {label}] Booked! ({count}/{shared['max_bookings']})")
                return {"success": True, "courts": [first, second], "dual": True}

            msg = result.get("message", "")

            if result.get("daily_limit") or is_daily_limit_error(msg):
                with shared["lock"]:
                    shared["daily_limit_reached"] = True
                log(f"[Dual {label}] Daily limit reached, stopping all")
                return {"success": False, "daily_limit": True}

            if result.get("same_slot_booked") or is_same_slot_booked_error(msg):
                with slot_lock:
                    slot_state["same_slot_booked"] = True
                log(f"[Dual {label}] Same slot already booked, skip this pair")
                return {"success": False, "same_slot_booked": True}

            log(f"[Dual {label}] Failed: {msg}, trying next court...")

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
        log(f"[Dual {label}] No more candidates")
    else:
        log(f"[Dual {label}] No same-court pair available in both slots")
    return {"success": False}


def _run_priority_wave(profile_id: int, duals: list[dict], singles: list[dict], shared: dict) -> list[dict]:
    tasks = [("dual", item) for item in duals] + [("single", item) for item in singles]
    if not tasks:
        return []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = []
        for kind, item in tasks:
            if kind == "dual":
                futures.append(executor.submit(_book_dual_slot, profile_id, item, shared))
            else:
                futures.append(executor.submit(
                    _book_time_slot,
                    profile_id,
                    item["time"],
                    item.get("fanout", 4),
                    item.get("parallel", 1),
                    shared,
                ))
        return [future.result() for future in futures]


# ==================== Full Booking Flow ====================

POOL_WAIT_AT_T_SECONDS = 5.0
POOL_WAIT_POLL_SECONDS = 0.05


def _pool_has_bookable(rt: ProfileRuntime, slot_times: list[str]) -> bool:
    return any(copy_slot_candidates(rt, *slot_times))


def run_booking_flow(profile_id: int, profile: dict, scheduled: bool = False):
    """
    Execute booking at T=0. Expects:
    - rt.client already set (login checks at T-150s/T-90s/T-30s)
    - rt.prefetched_captchas already filled (from T-6s prefetch)
    - rt.pool already filled (T-12h query, refined by the live query)
    Scheduled runs never block on a query: they book from the pool as it is.
    Manual runs (scheduled=False) query once before booking.
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

    dual_prefs = get_dual_slot_prefs(profile)
    occupied_times = dual_occupied_times(dual_prefs)
    single_prefs = filter_single_time_prefs(profile.get("time_prefs", []), occupied_times)
    max_bookings = profile.get("max_bookings", 2)
    slot_times = collect_query_slot_times(profile)
    booking_date = target_booking_date(profile, rt if scheduled else None)

    shared = {
        "successful": 0,
        "max_bookings": max_bookings,
        "daily_limit_reached": False,
        "booked_courts": [],
        "lock": threading.Lock(),
    }

    def flow_result(booked=None, persist=True):
        return {
            "persist_latest_result": persist,
            "latest_booking_result": build_latest_booking_result(booked or []) if persist else "",
            "booking_date": booking_date,
        }

    try:
        # Session: whatever the login checks left us with. T=0 never re-checks.
        if not rt.client:
            log("[T=0] No session available (login never succeeded), aborting booking")
            set_status("failed")
            return flow_result([])
        checked = (
            time.strftime("%H:%M:%S", time.localtime(rt.session_ready_checked_at))
            if rt.session_ready_checked_at else "never"
        )
        state = "valid" if rt.session_ready else "unverified"
        log(f"[T=0] Session {state} ({rt.session_ready_reason}, last check {checked})")

        if rt.cancel_event.is_set():
            set_status("idle")
            return flow_result(persist=False)

        ensure_pool(rt, profile, booking_date)
        if not scheduled:
            log("[T=0] Manual run: querying courts before booking...")
            do_query_candidates(profile_id, profile, source="manual", date=booking_date)

        if scheduled and not _pool_has_bookable(rt, slot_times):
            log(f"[T=0] Candidate pool has no bookable court yet, waiting up to {POOL_WAIT_AT_T_SECONDS:.0f}s for the live query...")
            deadline = time.time() + POOL_WAIT_AT_T_SECONDS
            while time.time() < deadline and not rt.cancel_event.is_set():
                if _pool_has_bookable(rt, slot_times):
                    break
                time.sleep(POOL_WAIT_POLL_SECONDS)

        snapshot = copy_slot_candidates(rt, *slot_times)
        with rt.candidates_lock:
            summary = pool_summary(rt.pool) or {}
        if not any(snapshot):
            log(
                f"[T=0] No bookable candidates (pool {summary.get('date')}: "
                f"{summary.get('selected', 0)} selected, {summary.get('unresolved', 0)} unresolved, "
                f"{summary.get('unavailable', 0)} unavailable, last ok {summary.get('last_success_at') or 'never'})"
            )
            set_status("failed")
            return flow_result([])

        total = sum(len(v) for v in snapshot)
        log(
            f"[T=0] Using {total} bookable candidates from pool "
            f"(last ok {summary.get('last_success_at') or '?'} via {summary.get('last_success_source') or '?'})"
        )

        if rt.cancel_event.is_set():
            set_status("idle")
            return flow_result(persist=False)

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
            return flow_result(persist=False)

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
                        return flow_result(persist=False)
                    time.sleep(0.1)

        # ---- START BOOKING ----
        log("--- Start booking ---")
        t_start = time.time()

        if dual_prefs:
            log("Dual slots: " + ", ".join(
                f"P{item['priority']}({item['time']}+{item['next_time']})"
                for item in dual_prefs
            ))
            if occupied_times:
                log("Single-slot road excludes: " + ", ".join(sorted(occupied_times)))
        if single_prefs:
            log("Single slots: " + ", ".join(
                f"P{item.get('priority', 1)}({item['time']})"
                for item in single_prefs
            ))

        sorted_priorities = collect_priority_levels(dual_prefs, single_prefs)
        log("Priority waves: " + " -> ".join(f"P{pri}" for pri in sorted_priorities))

        dual_road_succeeded = False
        for pri in sorted_priorities:
            if shared["successful"] >= max_bookings or shared["daily_limit_reached"]:
                break
            if rt.cancel_event.is_set():
                break

            duals, singles = tasks_for_wave(pri, dual_prefs, single_prefs, dual_road_succeeded)
            if not duals and not singles:
                continue

            dual_label = ", ".join(f"{item['time']}+{item['next_time']}" for item in duals) or "-"
            single_label = ", ".join(item["time"] for item in singles) or "-"
            log(f"--- Wave P{pri}: dual={dual_label} | single={single_label} ---")

            results = _run_priority_wave(profile_id, duals, singles, shared)
            if any(r.get("success") and r.get("dual") for r in results):
                dual_road_succeeded = True
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
        return flow_result(shared["booked_courts"])

    except Exception as e:
        log(f"Booking flow error: {e}")
        set_status("failed")
        return flow_result([])

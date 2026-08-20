"""
APScheduler job management: per-profile scheduled booking.

Timeline (matching userscript):
  T-60s   Login check (prepare authenticated session)
  T-6s    Prefetch 6 captchas anonymously (store for T=0)
  T-Xms   First court query + start 1s polling (anonymous, max 20 updates)
  T-2s    Final session validation
  T=0     Execute booking (use stored captchas + latest court data)
"""

import asyncio
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import database
import booking_engine
import log_manager

scheduler = AsyncIOScheduler()

_JOB_IDS = ["login", "captcha", "query", "precheck", "book"]
MAX_QUERY_UPDATES = 20
QUERY_POLL_INTERVAL_SECONDS = 1.0
QUERY_ALIGNMENT_TOLERANCE_SECONDS = 0.10
QUERY_SLEEP_CHUNK_SECONDS = 0.05
DEFAULT_CAPTCHA_PREFETCH_LEAD_SECONDS = 6
FINAL_SESSION_CHECK_LEAD_SECONDS = 2
LOGIN_LEAD_SECONDS = 60
LOGIN_CHECK_MAX_ATTEMPTS = 2
LOGIN_RETRY_DELAY_SECONDS = 0.5
_scheduling_lock: set[int] = set()  # Prevent concurrent schedule_profile calls
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def start():
    if not scheduler.running:
        scheduler.start()


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _normalize_schedule_weekdays(schedule_weekdays: list[int] | None) -> list[int]:
    if schedule_weekdays is None:
        return []
    normalized = sorted({
        int(day) for day in schedule_weekdays
        if isinstance(day, (int, str)) and str(day).isdigit() and 0 <= int(day) <= 6
    })
    return normalized


def _format_schedule_weekdays(schedule_weekdays: list[int]) -> str:
    if not schedule_weekdays:
        return "(none)"
    return ",".join(WEEKDAY_LABELS[day] for day in schedule_weekdays)


def _get_manual_target(schedule_time_str: str, now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now()
    parts = schedule_time_str.split(":")
    h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _get_resident_target(schedule_time_str: str, schedule_weekdays: list[int],
                         now: datetime | None = None) -> datetime | None:
    if now is None:
        now = datetime.now()
    weekdays = _normalize_schedule_weekdays(schedule_weekdays)
    if not weekdays:
        return None

    parts = schedule_time_str.split(":")
    h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0

    for offset in range(8):
        candidate_date = (now + timedelta(days=offset)).date()
        if candidate_date.weekday() not in weekdays:
            continue
        candidate = datetime.combine(candidate_date, datetime.min.time()).replace(
            hour=h, minute=m, second=s, microsecond=0)
        if candidate > now:
            return candidate
    return None


async def schedule_profile(profile_id: int, target: datetime | None = None, reason: str = "manual"):
    """Schedule the full booking timeline for a profile."""
    if profile_id in _scheduling_lock:
        return None  # Already scheduling, skip duplicate call
    _scheduling_lock.add(profile_id)
    try:
        return await _do_schedule_profile(profile_id, target=target, reason=reason)
    finally:
        _scheduling_lock.discard(profile_id)


async def _do_schedule_profile(profile_id: int, target: datetime | None = None, reason: str = "manual"):
    profile = await database.get_profile(profile_id)
    if not profile:
        return None

    cancel_profile(profile_id, cancel_runtime=False, cancel_booking_task=False)

    # Reset runtime state for new schedule
    rt = booking_engine.get_runtime(profile_id)
    rt.cancel_event.clear()
    booking_engine.reset_query_snapshot(rt)
    booking_engine.clear_prefetched_captchas(rt)
    booking_engine.mark_session_ready(rt, False, "not checked")

    schedule_time_str = profile.get("schedule_time", "08:40:00")
    pre_query_delay = profile.get("pre_query_delay", 1800)  # default 1.8s
    captcha_prefetch_lead_seconds = profile.get(
        "captcha_prefetch_lead_seconds", DEFAULT_CAPTCHA_PREFETCH_LEAD_SECONDS)

    now = datetime.now()
    if target is None:
        target = _get_manual_target(schedule_time_str, now=now)

    login_time = target - timedelta(seconds=LOGIN_LEAD_SECONDS)
    captcha_time = target - timedelta(seconds=captcha_prefetch_lead_seconds)
    query_time = target - timedelta(milliseconds=pre_query_delay)
    final_check_time = target - timedelta(seconds=FINAL_SESSION_CHECK_LEAD_SECONDS)

    rt = booking_engine.get_runtime(profile_id)
    rt.status = "waiting"
    await database.update_status(profile_id, "waiting")

    log_manager.emit(profile_id,
        f"[Schedule] mode={reason} | T={target.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"login@T-{LOGIN_LEAD_SECONDS}s({login_time.strftime('%H:%M:%S')}) | "
        f"captcha@T-{captcha_prefetch_lead_seconds}s({captcha_time.strftime('%H:%M:%S')}) | "
        f"query@T-{pre_query_delay}ms({query_time.strftime('%H:%M:%S.%f')[:-3]}) | "
        f"session@T-{FINAL_SESSION_CHECK_LEAD_SECONDS}s({final_check_time.strftime('%H:%M:%S')})",
        status="waiting")

    def _add_job(func, run_date, job_id, label, args=None):
        if args is None:
            args = [profile_id]
        if run_date > now:
            scheduler.add_job(func, trigger=DateTrigger(run_date=run_date),
                              id=job_id, args=args, replace_existing=True,
                              misfire_grace_time=60)
            log_manager.emit(profile_id, f"  Job [{label}] scheduled at {run_date.strftime('%H:%M:%S.%f')[:-3]}")
        else:
            scheduler.add_job(func, id=job_id, args=args, replace_existing=True,
                              misfire_grace_time=60)
            log_manager.emit(profile_id, f"  Job [{label}] running immediately (past {run_date.strftime('%H:%M:%S')})")

    _add_job(_run_login_check,      login_time,   f"login_{profile_id}",   "login")
    _add_job(_run_captcha_prefetch, captcha_time,  f"captcha_{profile_id}", "captcha")
    _add_job(
        _run_continuous_query,
        query_time,
        f"query_{profile_id}",
        "query",
        [profile_id, query_time.timestamp()],
    )
    _add_job(_run_final_session_check, final_check_time, f"precheck_{profile_id}", "session")
    _add_job(_run_booking,          target,        f"book_{profile_id}",    "booking")
    return target


def stop_query_polling(rt) -> None:
    """Stop spawning new ticks. In-flight queries keep running and may still apply by start time."""
    rt.continuous_query_running = False
    if getattr(rt, "query_task", None) and not rt.query_task.done():
        rt.query_task.cancel()
    rt.query_task = None


def cancel_profile(profile_id: int, cancel_runtime: bool = True, cancel_booking_task: bool = True):
    for prefix in _JOB_IDS:
        try:
            scheduler.remove_job(f"{prefix}_{profile_id}")
        except Exception:
            pass

    rt = booking_engine.get_runtime(profile_id)
    stop_query_polling(rt)
    if cancel_runtime:
        rt.cancel_event.set()
    if cancel_booking_task and rt.task and not rt.task.done():
        rt.task.cancel()


def get_next_run_time(profile_id: int) -> datetime | None:
    job = scheduler.get_job(f"book_{profile_id}")
    if not job or not job.next_run_time:
        return None
    next_run = job.next_run_time
    if next_run.tzinfo is not None:
        return next_run.astimezone().replace(tzinfo=None)
    return next_run


async def sync_resident_schedule(profile_id: int, profile: dict | None = None, reason: str = "resident"):
    if profile is None:
        profile = await database.get_profile(profile_id)
    if not profile:
        return None

    rt = booking_engine.get_runtime(profile_id)
    if rt.status == "running":
        return None

    if not profile.get("schedule_enabled"):
        if rt.status == "waiting":
            cancel_profile(profile_id)
            rt.status = "idle"
            await database.update_status(profile_id, "idle")
            log_manager.emit(profile_id, "[Schedule] Resident schedule disabled", status="idle")
        return None

    weekdays = _normalize_schedule_weekdays(profile.get("schedule_weekdays"))
    if not weekdays:
        cancel_profile(profile_id, cancel_runtime=False, cancel_booking_task=False)
        rt.status = "idle"
        await database.update_status(profile_id, "idle")
        log_manager.emit(profile_id, "[Schedule] Resident mode enabled but no weekdays selected", status="idle")
        return None

    target = _get_resident_target(profile.get("schedule_time", "08:40:00"), weekdays)
    if target is None:
        rt.status = "idle"
        await database.update_status(profile_id, "idle")
        log_manager.emit(
            profile_id,
            f"[Schedule] No future resident target found for weekdays={_format_schedule_weekdays(weekdays)}",
            status="idle",
        )
        return None

    log_manager.emit(
        profile_id,
        f"[Schedule] Resident weekdays={_format_schedule_weekdays(weekdays)} -> next {target.strftime('%Y-%m-%d %H:%M:%S')}",
    )
    return await schedule_profile(profile_id, target=target, reason=reason)


async def restore_enabled_profiles():
    for item in await database.list_profiles():
        profile = await database.get_profile(item["id"])
        if profile and profile.get("schedule_enabled"):
            await sync_resident_schedule(profile["id"], profile=profile, reason="startup")


# ==================== T-60s: Login Check ====================

async def _run_login_check(profile_id: int):
    profile = await database.get_profile(profile_id)
    if not profile:
        return
    rt = booking_engine.get_runtime(profile_id)
    if rt.cancel_event.is_set():
        return

    log_manager.emit(profile_id, f"[T-{LOGIN_LEAD_SECONDS}s] Checking login status...")

    def _check():
        if rt.client:
            log_manager.emit(profile_id, f"[T-{LOGIN_LEAD_SECONDS}s] Session exists, verifying with /web/order/seachMyOrder.html...")
            try:
                valid, reason = booking_engine.verify_booking_session(rt.client)
                if valid:
                    log_manager.emit(profile_id, f"[T-{LOGIN_LEAD_SECONDS}s] Session valid ({reason})")
                    return
                else:
                    log_manager.emit(profile_id, f"[T-{LOGIN_LEAD_SECONDS}s] Session invalid ({reason}), re-login...")
            except Exception as e:
                log_manager.emit(profile_id, f"[T-{LOGIN_LEAD_SECONDS}s] Session check failed: {e}, re-login...")

        for attempt in range(1, LOGIN_CHECK_MAX_ATTEMPTS + 1):
            if booking_engine.do_login(profile_id, profile["username"], profile["password"], profile.get("booking_channel")):
                if attempt > 1:
                    log_manager.emit(
                        profile_id,
                        f"[T-{LOGIN_LEAD_SECONDS}s] Login recovered on retry {attempt}/{LOGIN_CHECK_MAX_ATTEMPTS}"
                    )
                return

            if attempt < LOGIN_CHECK_MAX_ATTEMPTS:
                log_manager.emit(
                    profile_id,
                    f"[T-{LOGIN_LEAD_SECONDS}s] Login attempt {attempt}/{LOGIN_CHECK_MAX_ATTEMPTS} failed, retrying..."
                )
                time.sleep(LOGIN_RETRY_DELAY_SECONDS)

        log_manager.emit(
            profile_id,
            f"[T-{LOGIN_LEAD_SECONDS}s] Login failed after {LOGIN_CHECK_MAX_ATTEMPTS} attempts"
        )

    await asyncio.to_thread(_check)


# ==================== T-6s: Prefetch Captchas ====================

async def _run_captcha_prefetch(profile_id: int):
    rt = booking_engine.get_runtime(profile_id)
    if rt.cancel_event.is_set():
        return
    await asyncio.to_thread(booking_engine.do_prefetch_captchas, profile_id, 6)


# ==================== T-Xms: Continuous Court Query (Anonymous) ====================

async def _run_continuous_query(profile_id: int, query_anchor_ts: float | None = None):
    """Start aligned court polling. Each query starts on the next 1s tick from the anchor."""
    profile = await database.get_profile(profile_id)
    if not profile:
        return
    rt = booking_engine.get_runtime(profile_id)
    if rt.cancel_event.is_set():
        return

    rt.continuous_query_running = True
    if query_anchor_ts is None:
        query_anchor_ts = time.time()
    log_manager.emit(profile_id,
        f"[T-Xms] Starting court query (anonymous, 1s interval, max {MAX_QUERY_UPDATES} updates)...")

    async def _sleep_until(target_ts: float):
        while True:
            if not rt.continuous_query_running or rt.cancel_event.is_set():
                return False
            remaining = target_ts - time.time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(remaining, QUERY_SLEEP_CHUNK_SECONDS))

    async def _query_once(seq: int, started_at: float, attempt_no: int):
        try:
            await asyncio.to_thread(
                booking_engine.do_query_candidates, profile_id, profile, seq, started_at
            )
        except Exception as e:
            log_manager.emit(profile_id, f"[Query #{attempt_no}] Error: {e}")

    async def _poll():
        attempts = 0
        tick_index = 0
        while rt.continuous_query_running and not rt.cancel_event.is_set() and attempts < MAX_QUERY_UPDATES:
            target_ts = query_anchor_ts + tick_index * QUERY_POLL_INTERVAL_SECONDS
            now_ts = time.time()

            if tick_index == 0:
                if target_ts > now_ts and not await _sleep_until(target_ts):
                    break
            else:
                drift = now_ts - target_ts
                if drift < -QUERY_ALIGNMENT_TOLERANCE_SECONDS:
                    if not await _sleep_until(target_ts):
                        break
                elif drift > QUERY_ALIGNMENT_TOLERANCE_SECONDS:
                    next_tick_index = int((now_ts - query_anchor_ts) // QUERY_POLL_INTERVAL_SECONDS) + 1
                    next_tick_ts = query_anchor_ts + next_tick_index * QUERY_POLL_INTERVAL_SECONDS
                    log_manager.emit(
                        profile_id,
                        f"[Query] Missed aligned tick by {drift * 1000:.0f}ms, "
                        f"waiting for next tick {datetime.fromtimestamp(next_tick_ts).strftime('%H:%M:%S.%f')[:-3]}",
                    )
                    tick_index = next_tick_index
                    if not await _sleep_until(next_tick_ts):
                        break
            attempt_no = attempts + 1
            seq, started_at = booking_engine.begin_query_seq(rt)
            asyncio.create_task(
                _query_once(seq, started_at, attempt_no),
                name=f"query-{profile_id}-{seq}",
            )
            attempts += 1
            tick_index += 1
        if attempts >= MAX_QUERY_UPDATES:
            log_manager.emit(profile_id, f"[Query] Reached max {MAX_QUERY_UPDATES} updates, stopping polling")

    rt.query_task = asyncio.create_task(_poll())


# ==================== T-2s: Final Session Check ====================

async def _run_final_session_check(profile_id: int):
    rt = booking_engine.get_runtime(profile_id)
    if rt.cancel_event.is_set():
        return

    def _check():
        if not rt.client:
            booking_engine.mark_session_ready(rt, False, "no client")
            log_manager.emit(profile_id, "[T-2s] No session available for final validation")
            return

        valid, reason = booking_engine.verify_booking_session(rt.client)
        booking_engine.mark_session_ready(rt, valid, reason)

        if valid:
            log_manager.emit(profile_id, f"[T-2s] Session ready ({reason})")
            return

        try:
            rt.client.close()
        except Exception:
            pass
        rt.client = None
        log_manager.emit(profile_id, f"[T-2s] Session invalid ({reason})")

    await asyncio.to_thread(_check)


# ==================== T=0: Execute Booking ====================

async def _run_booking(profile_id: int):
    profile = await database.get_profile(profile_id)
    if not profile:
        return

    rt = booking_engine.get_runtime(profile_id)
    rt.cancel_event.clear()

    now = datetime.now()
    log_manager.emit(profile_id,
        f"[T=0] Booking time! {now.strftime('%H:%M:%S.%f')[:-3]}",
        status="running")

    await database.update_status(profile_id, "running")
    result = await asyncio.to_thread(booking_engine.run_booking_flow, profile_id, profile)

    # Stop spawning new ticks; in-flight queries may still update the snapshot.
    stop_query_polling(rt)

    if result and result.get("persist_latest_result"):
        await database.record_booking_result(
            profile_id,
            result.get("booking_date") or booking_engine.get_target_booking_date(profile),
            result.get("latest_booking_result", ""),
            unread=True,
        )
    await database.update_status(profile_id, rt.status)
    latest_profile = await database.get_profile(profile_id)
    if latest_profile and latest_profile.get("schedule_enabled"):
        await sync_resident_schedule(profile_id, profile=latest_profile, reason="post-run")

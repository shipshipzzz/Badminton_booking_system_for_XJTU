"""FastAPI application: REST + WebSocket + lifecycle + auth."""

import asyncio
import os
import sys
import secrets
from datetime import date, timedelta

# Ensure cas_http is importable
_cas_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cas_http")
if _cas_path not in sys.path:
    sys.path.insert(0, _cas_path)
from typing import Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

import database
import log_manager
import booking_engine
import scheduler
from pydantic import BaseModel
from models import ProfileCreate, ProfileUpdate, ProfileSummary, ProfileResponse, StartRequest


# ==================== Auth ====================

ADMIN_USERNAME = os.getenv("BOOKING_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("BOOKING_ADMIN_PASSWORD")

# In-memory session store: {token: {"username": str, "is_admin": bool}}
_sessions: dict[str, dict] = {}


class LoginRequest(BaseModel):
    username: str
    password: str


def get_current_user(request: Request) -> dict:
    """Extract and validate auth token from request header."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token or token not in _sessions:
        raise HTTPException(401, "Not authenticated")
    return _sessions[token]


def require_profile_access(user: dict, profile_id: int, profile: dict):
    """Check if user can access this profile."""
    if user["is_admin"]:
        return  # Admin can access all
    # Regular user: can only access profiles with their username
    if profile.get("username") != user["username"]:
        raise HTTPException(403, "Access denied")


def require_admin(user: dict):
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")


# ==================== Lifespan ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    scheduler.start()
    await scheduler.restore_enabled_profiles()
    relay_task = asyncio.create_task(log_manager.relay_loop())
    yield
    relay_task.cancel()
    try:
        await relay_task
    except asyncio.CancelledError:
        pass
    scheduler.stop()


app = FastAPI(title="Badminton Booking Manager", lifespan=lifespan)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _format_next_schedule_at(profile_id: int) -> str | None:
    next_run = scheduler.get_next_run_time(profile_id)
    if not next_run:
        return None
    return next_run.strftime("%Y-%m-%d %H:%M:%S")


RECENT_BOOKING_DAYS = 3


def _recent_booking_dates() -> list[date]:
    today = date.today()
    return [today + timedelta(days=i) for i in range(RECENT_BOOKING_DAYS)]


def _target_day_offset(profile: dict) -> int:
    target_days = profile.get("target_days") or [2]
    try:
        return int(target_days[0])
    except (TypeError, ValueError, IndexError):
        return 2


def _program_enabled_for_booking_date(profile: dict, booking_date: date) -> bool:
    if not profile.get("schedule_enabled"):
        return False
    weekdays = profile.get("schedule_weekdays") or []
    try:
        weekdays = {int(day) for day in weekdays}
    except (TypeError, ValueError):
        return False
    run_date = booking_date - timedelta(days=_target_day_offset(profile))
    return run_date.weekday() in weekdays


async def _build_recent_booking_summary(profile: dict) -> str:
    days = _recent_booking_dates()
    date_strings = [day.isoformat() for day in days]
    statuses = await database.list_booking_day_statuses(profile["id"], date_strings)
    lines = []
    for day in days:
        day_key = day.isoformat()
        result = (statuses.get(day_key) or "").strip()
        lines.append(day_key)
        if result:
            details = "\n   ".join(result.splitlines())
            lines.append("1. 抢票程序已执行")
            lines.append(f"2. 抢到的场地：\n   {details}")
        elif _program_enabled_for_booking_date(profile, day):
            lines.append("1. 抢票程序已启用，暂无执行结果")
            lines.append("2. 抢到的场地：暂无记录")
        else:
            lines.append("1. 未启用抢票程序")
            lines.append("2. 抢到的场地：无")
        lines.append("")
    return "\n".join(lines).strip()


async def _attach_profile_runtime(profile: dict) -> dict:
    profile = dict(profile)
    rt = booking_engine.get_runtime(profile["id"])
    profile["status"] = rt.status
    profile["next_schedule_at"] = _format_next_schedule_at(profile["id"])
    profile["latest_booking_result"] = await _build_recent_booking_summary(profile)
    return profile


@app.get("/")
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ==================== Auth API ====================

@app.post("/api/login")
async def login(body: LoginRequest):
    # Admin login
    if ADMIN_PASSWORD and body.username == ADMIN_USERNAME and body.password == ADMIN_PASSWORD:
        token = secrets.token_hex(32)
        _sessions[token] = {"username": ADMIN_USERNAME, "is_admin": True}
        return {"token": token, "username": ADMIN_USERNAME, "is_admin": True}

    # Regular user: match against any profile's CAS username + password
    profiles = await database.list_profiles_full()
    matched = None
    for p in profiles:
        if p["username"] == body.username and p["password"] == body.password:
            matched = p
            break

    if not matched:
        raise HTTPException(401, "Invalid username or password")

    token = secrets.token_hex(32)
    _sessions[token] = {"username": body.username, "is_admin": False}
    return {"token": token, "username": body.username, "is_admin": False}


@app.post("/api/logout")
async def logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    _sessions.pop(token, None)
    return {"ok": True}


@app.get("/api/me")
async def get_me(request: Request):
    user = get_current_user(request)
    return {"username": user["username"], "is_admin": user["is_admin"]}


# ==================== Registration + Invite Codes ====================

class RegisterRequest(BaseModel):
    username: str
    password: str
    invite_code: str


def _verify_cas_login_sync(username: str, password: str) -> bool:
    from cas_login import CASLogin
    import time

    for attempt in range(1, 4):
        try:
            cas = CASLogin(timeout=20)
            client = cas.login(username, password)
            client.close()
            return True
        except Exception:
            if attempt < 3:
                time.sleep(0.5)
    return False


@app.post("/api/register")
async def register(body: RegisterRequest):
    """Register with CAS credentials + invite code. Verifies CAS login (3 attempts)."""
    if not body.username or not body.password or not body.invite_code:
        raise HTTPException(400, "All fields required")

    # Check invite code is valid and unused (don't consume yet)
    codes = await database.list_invite_codes()
    valid_code = any(c["code"] == body.invite_code and c["used_by"] is None for c in codes)
    if not valid_code:
        raise HTTPException(400, "Invalid or already used invite code")

    # Check if username already has a profile
    profiles = await database.list_profiles_full()
    if any(p["username"] == body.username for p in profiles):
        raise HTTPException(400, "This account is already registered")

    success = await asyncio.to_thread(
        _verify_cas_login_sync,
        body.username,
        body.password,
    )

    if not success:
        raise HTTPException(401, "CAS login failed. Please check your credentials.")

    # CAS login succeeded — consume invite code
    used = await database.use_invite_code(body.invite_code, body.username)
    if not used:
        raise HTTPException(400, "Invite code was just used by someone else")

    # Create profile
    profile = await database.create_profile(
        name=body.username, username=body.username, password=body.password)

    return {"ok": True, "message": "Registration successful", "profile_id": profile["id"]}


# Admin: Invite code management

@app.get("/api/invite-codes")
async def list_invite_codes(request: Request):
    user = get_current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    return await database.list_invite_codes()


@app.post("/api/invite-codes")
async def create_invite_code(request: Request):
    user = get_current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    code = secrets.token_hex(4).upper()  # 8-char hex code
    await database.create_invite_code(code)
    return {"code": code}


@app.delete("/api/invite-codes/{code}")
async def delete_invite_code(code: str, request: Request):
    user = get_current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    ok = await database.delete_invite_code(code)
    if not ok:
        raise HTTPException(404, "Code not found")
    return {"ok": True}


# ==================== Change Password ====================

class ChangePasswordRequest(BaseModel):
    new_password: str


class CloneProfileRequest(BaseModel):
    name: Optional[str] = None


@app.post("/api/profiles/{profile_id}/change-password")
async def change_password(profile_id: int, body: ChangePasswordRequest, request: Request):
    """User changes their own CAS password. Verifies new password via CAS login."""
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)

    if not body.new_password:
        raise HTTPException(400, "Password cannot be empty")

    username = profile["username"]
    success = await asyncio.to_thread(
        _verify_cas_login_sync,
        username,
        body.new_password,
        profile_id,
    )

    if not success:
        raise HTTPException(401, "New password CAS login failed. Password not changed.")

    # Update password in DB
    await database.update_profile(profile_id, {"password": body.new_password})

    # Update session token to use new password for login matching
    return {"ok": True, "message": "Password updated successfully"}


# ==================== Profile CRUD ====================

@app.get("/api/profiles")
async def list_profiles(request: Request):
    user = get_current_user(request)
    profiles = await database.list_profiles()
    # Filter for regular users
    if not user["is_admin"]:
        profiles = [p for p in profiles if p.get("username") == user["username"]]
    return [await _attach_profile_runtime(p) for p in profiles]


@app.post("/api/profiles")
async def create_profile(body: ProfileCreate, request: Request):
    user = get_current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only admin can create profiles")
    profile = await database.create_profile(body.name, body.username, body.password)
    if body.group_name:
        profile = await database.update_profile(profile["id"], {"group_name": body.group_name})
    return await _attach_profile_runtime(profile)


@app.get("/api/profiles/{profile_id}")
async def get_profile(profile_id: int, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)
    return await _attach_profile_runtime(profile)


@app.post("/api/profiles/{profile_id}/clone")
async def clone_profile(profile_id: int, body: CloneProfileRequest | None = None, request: Request = None):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)

    new_name = body.name.strip() if body and body.name and body.name.strip() else None
    cloned = await database.clone_profile(profile_id, new_name)
    if not cloned:
        raise HTTPException(404, "Profile not found")
    return await _attach_profile_runtime(cloned)


@app.post("/api/profiles/{profile_id}/latest-booking/read")
async def clear_latest_booking_result(profile_id: int, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)
    await database.clear_latest_booking_result(profile_id)
    return await _attach_profile_runtime(await database.get_profile(profile_id))


@app.put("/api/profiles/{profile_id}")
async def update_profile(profile_id: int, body: ProfileUpdate, request: Request):
    user = get_current_user(request)
    existing = await database.get_profile(profile_id)
    if not existing:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, existing)
    updates = body.model_dump(exclude_none=True)
    if "venue_prefs" in updates:
        updates["venue_prefs"] = [v.model_dump() if hasattr(v, "model_dump") else v for v in updates["venue_prefs"]]
    if "time_prefs" in updates:
        updates["time_prefs"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in updates["time_prefs"]]
    # Regular users cannot change username/password
    if not user["is_admin"]:
        updates.pop("username", None)
        updates.pop("password", None)
    profile = await database.update_profile(profile_id, updates)
    if not profile:
        raise HTTPException(404, "Profile not found")
    rt = booking_engine.get_runtime(profile_id)
    schedule_changed = any(
        key in updates
        for key in ("schedule_enabled", "schedule_weekdays", "schedule_time")
    )
    if profile.get("schedule_enabled"):
        if rt.status != "running":
            await scheduler.sync_resident_schedule(profile_id, profile=profile, reason="update")
    elif existing.get("schedule_enabled") and schedule_changed and rt.status != "running":
        scheduler.cancel_profile(profile_id)
        rt.status = "idle"
        await database.update_status(profile_id, "idle")
        log_manager.emit(profile_id, "[Schedule] Resident schedule disabled by config update", status="idle")
    return await _attach_profile_runtime(await database.get_profile(profile_id))


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: int, request: Request):
    user = get_current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only admin can delete profiles")
    scheduler.cancel_profile(profile_id)
    rt = booking_engine.get_runtime(profile_id)
    if rt.task and not rt.task.done():
        rt.cancel_event.set()
        try:
            await asyncio.wait_for(rt.task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    booking_engine.remove_runtime(profile_id)
    log_manager.remove_profile_log(profile_id)
    ok = await database.delete_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"ok": True}


# ==================== Profile Actions ====================

@app.post("/api/profiles/{profile_id}/login")
async def test_login(profile_id: int, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)
    if not profile["username"] or not profile["password"]:
        raise HTTPException(400, "Username and password required")
    success = await asyncio.to_thread(
        booking_engine.do_login, profile_id, profile["username"], profile["password"]
    )
    return {"success": success}


@app.post("/api/profiles/{profile_id}/start")
async def start_booking(profile_id: int, body: StartRequest = StartRequest(), request: Request = None):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)
    if not profile["username"] or not profile["password"]:
        raise HTTPException(400, "Username and password required")
    rt = booking_engine.get_runtime(profile_id)
    if rt.status in ("running", "waiting"):
        raise HTTPException(409, f"Already {rt.status}")
    if body.immediate:
        rt.cancel_event.clear()
        rt.status = "running"
        await database.update_status(profile_id, "running")
        async def _run():
            try:
                result = await asyncio.to_thread(booking_engine.run_booking_flow, profile_id, profile)
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
                    rt.task = None
                    await scheduler.sync_resident_schedule(profile_id, profile=latest_profile, reason="post-run")
            finally:
                rt.task = None
        rt.task = asyncio.create_task(_run())
        return {"status": "running", "mode": "immediate"}
    else:
        target = await scheduler.schedule_profile(profile_id)
        return {
            "status": "waiting",
            "mode": "scheduled",
            "time": profile["schedule_time"],
            "next_schedule_at": target.strftime("%Y-%m-%d %H:%M:%S") if target else None,
        }


class BookCourtRequest(BaseModel):
    venue_id: int
    venue_name: str
    date: str
    time_slot: str
    stock_id: int
    court_number: int
    seat_id: str


@app.post("/api/profiles/{profile_id}/book-court")
async def book_single_court(profile_id: int, body: BookCourtRequest, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)
    if not profile["username"] or not profile["password"]:
        raise HTTPException(400, "Username and password required")
    rt = booking_engine.get_runtime(profile_id)
    if not rt.client:
        log_manager.emit(profile_id, "Logging in for single court booking...")
        success = await asyncio.to_thread(
            booking_engine.do_login, profile_id, profile["username"], profile["password"])
        if not success:
            raise HTTPException(500, "Login failed")
    court = {
        "venueId": body.venue_id, "venueName": body.venue_name,
        "date": body.date, "timeSlot": body.time_slot, "stockId": body.stock_id,
        "court": {"courtNumber": body.court_number, "courtName": f"场地{body.court_number}", "seatId": body.seat_id},
    }
    log_manager.emit(profile_id, f"[Manual] Booking {body.venue_name} {body.time_slot} 场地{body.court_number}...")
    result = await asyncio.to_thread(booking_engine.do_book_court, profile_id, court)
    if result.get("success"):
        await database.record_booking_result(
            profile_id,
            body.date,
            booking_engine.build_latest_booking_result([court]),
            unread=True,
        )
    return result


@app.post("/api/profiles/{profile_id}/stop")
async def stop_booking(profile_id: int, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)
    scheduler.cancel_profile(profile_id)
    rt = booking_engine.get_runtime(profile_id)
    rt.cancel_event.set()
    rt.status = "idle"
    await database.update_status(profile_id, "idle")
    log_manager.emit(profile_id, "Stopped by user")
    return {"status": "idle"}


# ==================== Log History ====================

@app.get("/api/profiles/{profile_id}/logs")
async def list_log_files(profile_id: int, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if profile:
        require_profile_access(user, profile_id, profile)
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    if not os.path.isdir(log_dir):
        return []
    prefix = f"profile_{profile_id}_"
    files = sorted(
        [f for f in os.listdir(log_dir) if f.startswith(prefix) and f.endswith(".log")],
        reverse=True,
    )
    result = []
    for f in files:
        session_id = f.replace(prefix, "").replace(".log", "")
        path = os.path.join(log_dir, f)
        size = os.path.getsize(path)
        parts = session_id.split("_")
        display = f"{parts[0]} {parts[1].replace('-', ':')}" if len(parts) == 2 else session_id
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = sum(1 for _ in fh)
        except Exception:
            lines = 0
        result.append({"session": session_id, "display": display, "size": size, "lines": lines})
    return result


@app.get("/api/profiles/{profile_id}/logs/{session}")
async def get_log_content(profile_id: int, session: str, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if profile:
        require_profile_access(user, profile_id, profile)
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    log_file = os.path.join(log_dir, f"profile_{profile_id}_{session}.log")
    if not os.path.isfile(log_file):
        raise HTTPException(404, "Log file not found")
    with open(log_file, "r", encoding="utf-8") as f:
        content = f.read()
    return {"session": session, "content": content}


# ==================== My Orders ====================

async def _ensure_profile_booking_session(profile_id: int, profile: dict, *,
                                          relogin_if_invalid: bool,
                                          log_prefix: str):
    rt = booking_engine.get_runtime(profile_id)
    session_valid = False
    session_reason = "no client"
    if rt.client:
        session_valid, session_reason = await asyncio.to_thread(
            booking_engine.verify_booking_session, rt.client
        )

    if session_valid:
        return booking_engine.get_runtime(profile_id)

    if not relogin_if_invalid and rt.client:
        raise HTTPException(401, f"Booking session invalid: {session_reason}")

    if not profile.get("username") or not profile.get("password"):
        raise HTTPException(400, "Missing account credentials")

    log_manager.emit(profile_id, f"{log_prefix} Session missing/invalid ({session_reason}), auto-login...")
    success = await asyncio.to_thread(
        booking_engine.do_login, profile_id, profile["username"], profile["password"]
    )
    if not success:
        raise HTTPException(500, "Auto login failed")
    return booking_engine.get_runtime(profile_id)




def _fetch_orders_once(client):
    host = "http://202.117.17.144"
    headers = {"X-Requested-With": "XMLHttpRequest"}
    status_map = {1: "完成", 2: "已取消", 3: "待支付"}
    resp = client.get(
        f"{host}/order/seachMyOrder.html",
        params={"page": 1, "rows": 20, "sort": "createdate", "order": "desc"},
        headers=headers,
        timeout=10,
        follow_redirects=False,
    )
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "")
        raise RuntimeError(f"session invalid: redirect {resp.status_code} -> {location[:120]}")
    if resp.status_code != 200:
        raise RuntimeError(f"orders query failed: status={resp.status_code}")

    try:
        data = resp.json()
    except Exception as e:
        text = resp.text[:120].replace("\r", " ").replace("\n", " ")
        raise RuntimeError(f"session invalid: non-json orders response ({text})") from e

    if not isinstance(data, dict) or ("rows" not in data and "total" not in data):
        raise RuntimeError(f"session invalid: unexpected orders payload ({str(data)[:120]})")

    orders = []
    for row in data.get("rows", []):
        order_id = row.get("orderid")
        order = {
            "orderId": order_id,
            "venue": row.get("servicenames"),
            "createTime": row.get("createdate"),
            "status": status_map.get(row.get("status"), str(row.get("status"))),
            "date": "",
            "timeSlot": "",
            "court": "",
        }
        try:
            dr = client.get(
                f"{host}/order/seachData.html",
                params={"orderid": order_id, "page": 1, "rows": 1},
                headers=headers,
                timeout=5,
                follow_redirects=False,
            )
            if dr.status_code == 200:
                dd = dr.json()
                if dd.get("rows"):
                    r0 = dd["rows"][0]
                    order["date"] = (r0.get("stock") or {}).get("s_date", "")
                    order["timeSlot"] = (r0.get("stock") or {}).get("time_no", "")
                    order["court"] = (r0.get("stockdetail") or {}).get("sname", "")
        except Exception:
            pass
        orders.append(order)
    return orders


@app.get("/api/profiles/{profile_id}/orders")
async def get_my_orders(profile_id: int, request: Request):
    user = get_current_user(request)
    profile = await database.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    require_profile_access(user, profile_id, profile)

    rt = await _ensure_profile_booking_session(
        profile_id, profile, relogin_if_invalid=True, log_prefix="[Orders]"
    )
    try:
        return await asyncio.to_thread(_fetch_orders_once, rt.client)
    except RuntimeError as e:
        err = str(e)
        if "session invalid:" not in err:
            raise HTTPException(502, err) from e

    log_manager.emit(profile_id, f"[Orders] Order query detected expired session ({err}), retrying after auto-login...")
    rt = await _ensure_profile_booking_session(
        profile_id, profile, relogin_if_invalid=True, log_prefix="[Orders][Retry]"
    )
    try:
        return await asyncio.to_thread(_fetch_orders_once, rt.client)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e


# ==================== Venue Query (stateless, no auth needed) ====================

@app.get("/api/venues/query")
async def query_venues(venue_id: int, date: str):
    import sys as _sys
    _cas_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cas_http")
    if _cas_path not in _sys.path:
        _sys.path.insert(0, _cas_path)
    import httpx
    from booking_api import query_times as _qt, query_seats as _qs

    def _query():
        client = httpx.Client(timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        try:
            times = _qt(client, venue_id, date)
            result = []
            for t in times:
                seats = _qs(client, venue_id, t["ID"])
                result.append({"time": t["TIME_NO"], "stockId": t["ID"],
                               "surplus": t.get("SURPLUS", 0), "seats": seats})
            return result
        finally:
            client.close()

    return await asyncio.to_thread(_query)


# ==================== WebSocket ====================

@app.websocket("/ws/logs/{profile_id}")
async def ws_logs(websocket: WebSocket, profile_id: int):
    await websocket.accept()
    plog = log_manager.get_profile_log(profile_id)
    plog.ws_clients.add(websocket)
    try:
        for entry in plog.buffer:
            await websocket.send_json(entry)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        plog.ws_clients.discard(websocket)


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    log_manager.add_dashboard_client(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        log_manager.remove_dashboard_client(websocket)


# ==================== Entry ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

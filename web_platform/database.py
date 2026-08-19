"""SQLite database: schema init + profile CRUD."""

import json
import os
import aiosqlite

from models import default_venue_prefs, default_time_prefs

DB_PATH = os.path.join(os.path.dirname(__file__), "bookings.db")
DEFAULT_SCHEDULE_WEEKDAYS = list(range(7))

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT 'New Profile',
    username        TEXT NOT NULL DEFAULT '',
    password        TEXT NOT NULL DEFAULT '',
    target_days     TEXT NOT NULL DEFAULT '[2]',
    venue_prefs     TEXT NOT NULL DEFAULT '[]',
    time_prefs      TEXT NOT NULL DEFAULT '[]',
    dual_slot_enabled INTEGER NOT NULL DEFAULT 0,
    dual_slot_prefs TEXT NOT NULL DEFAULT '[]',
    schedule_enabled INTEGER NOT NULL DEFAULT 0,
    schedule_weekdays TEXT NOT NULL DEFAULT '[0, 1, 2, 3, 4, 5, 6]',
    schedule_time   TEXT NOT NULL DEFAULT '08:40:00',
    schedule_mode   TEXT NOT NULL DEFAULT 'api',
    pre_query_delay INTEGER NOT NULL DEFAULT 1200,
    max_bookings    INTEGER NOT NULL DEFAULT 2,
    booking_channel TEXT NOT NULL DEFAULT '8080',
    group_name      TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'idle',
    latest_booking_result TEXT NOT NULL DEFAULT '',
    latest_booking_result_at TEXT DEFAULT NULL,
    latest_booking_result_unread INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    used_by     TEXT DEFAULT NULL,
    used_at     TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS booking_day_statuses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   INTEGER NOT NULL,
    booking_date TEXT NOT NULL,
    result       TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(profile_id, booking_date)
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await _migrate_profiles_table(db)
        await db.commit()


async def _migrate_profiles_table(db: aiosqlite.Connection):
    cursor = await db.execute("PRAGMA table_info(profiles)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "schedule_weekdays" not in columns:
        default_weekdays = json.dumps(DEFAULT_SCHEDULE_WEEKDAYS)
        await db.execute(
            f"ALTER TABLE profiles ADD COLUMN schedule_weekdays TEXT NOT NULL DEFAULT '{default_weekdays}'"
        )
    if "group_name" not in columns:
        await db.execute(
            "ALTER TABLE profiles ADD COLUMN group_name TEXT NOT NULL DEFAULT ''"
        )
    if "latest_booking_result" not in columns:
        await db.execute(
            "ALTER TABLE profiles ADD COLUMN latest_booking_result TEXT NOT NULL DEFAULT ''"
        )
    if "latest_booking_result_at" not in columns:
        await db.execute(
            "ALTER TABLE profiles ADD COLUMN latest_booking_result_at TEXT DEFAULT NULL"
        )
    if "latest_booking_result_unread" not in columns:
        await db.execute(
            "ALTER TABLE profiles ADD COLUMN latest_booking_result_unread INTEGER NOT NULL DEFAULT 0"
        )
    if "booking_channel" not in columns:
        await db.execute(
            "ALTER TABLE profiles ADD COLUMN booking_channel TEXT NOT NULL DEFAULT '8080'"
        )
    if "dual_slot_enabled" not in columns:
        await db.execute(
            "ALTER TABLE profiles ADD COLUMN dual_slot_enabled INTEGER NOT NULL DEFAULT 0"
        )
    if "dual_slot_prefs" not in columns:
        await db.execute(
            "ALTER TABLE profiles ADD COLUMN dual_slot_prefs TEXT NOT NULL DEFAULT '[]'"
        )
    await db.execute(
        """
        UPDATE profiles
        SET schedule_weekdays = ?
        WHERE schedule_weekdays IS NULL OR TRIM(schedule_weekdays) = ''
        """,
        (json.dumps(DEFAULT_SCHEDULE_WEEKDAYS),),
    )
    await db.execute(
        """
        UPDATE profiles
        SET latest_booking_result = ''
        WHERE latest_booking_result IS NULL
        """
    )
    await db.execute(
        """
        UPDATE profiles
        SET group_name = ''
        WHERE group_name IS NULL
        """
    )
    await db.execute(
        """
        UPDATE profiles
        SET latest_booking_result_unread = 0
        WHERE latest_booking_result_unread IS NULL
        """
    )
    await db.execute(
        """
        UPDATE profiles
        SET dual_slot_enabled = 0
        WHERE dual_slot_enabled IS NULL
        """
    )
    await db.execute(
        """
        UPDATE profiles
        SET dual_slot_prefs = '[]'
        WHERE dual_slot_prefs IS NULL OR TRIM(dual_slot_prefs) = ''
        """
    )


# ==================== Invite Codes ====================

async def create_invite_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO invite_codes (code) VALUES (?)", (code,))
        await db.commit()


async def list_invite_codes() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT code, created_at, used_by, used_at FROM invite_codes ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [{"code": r[0], "created_at": r[1], "used_by": r[2], "used_at": r[3]} for r in rows]


async def use_invite_code(code: str, username: str) -> bool:
    """Mark invite code as used. Returns True if code was valid and unused."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE invite_codes SET used_by = ?, used_at = datetime('now','localtime') WHERE code = ? AND used_by IS NULL",
            (username, code))
        await db.commit()
        return cursor.rowcount > 0


async def delete_invite_code(code: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM invite_codes WHERE code = ?", (code,))
        await db.commit()
        return cursor.rowcount > 0


def _row_to_dict(row, columns) -> dict:
    d = dict(zip(columns, row))
    for key in ("target_days", "venue_prefs", "time_prefs", "schedule_weekdays", "dual_slot_prefs"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                d[key] = []
    d["schedule_enabled"] = bool(d.get("schedule_enabled", 0))
    d["dual_slot_enabled"] = bool(d.get("dual_slot_enabled", 0))
    d["latest_booking_result_unread"] = bool(d.get("latest_booking_result_unread", 0))
    channel = str(d.get("booking_channel") or "8080").strip()
    d["booking_channel"] = "80" if channel in ("80", "pc", "web", "desktop") else "8080"
    if "dual_slot_prefs" not in d or d["dual_slot_prefs"] is None:
        d["dual_slot_prefs"] = []
    if "schedule_weekdays" not in d or d["schedule_weekdays"] is None:
        d["schedule_weekdays"] = list(DEFAULT_SCHEDULE_WEEKDAYS)
    return d


async def list_profiles() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, name, username, group_name, status, booking_channel,
                   target_days, schedule_enabled, schedule_weekdays,
                   latest_booking_result, latest_booking_result_at, latest_booking_result_unread
            FROM profiles
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [_row_to_dict(row, columns) for row in rows]


async def list_profiles_full() -> list[dict]:
    """List all profiles with username + password (for auth matching)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, username, password FROM profiles ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [{"id": r[0], "username": r[1], "password": r[2]} for r in rows]


async def get_profile(profile_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cursor.description]
        return _row_to_dict(row, columns)


async def create_profile(name: str = "New Profile", username: str = "", password: str = "") -> dict:
    venue_prefs = json.dumps(default_venue_prefs(), ensure_ascii=False)
    time_prefs = json.dumps(default_time_prefs(), ensure_ascii=False)
    schedule_weekdays = json.dumps(DEFAULT_SCHEDULE_WEEKDAYS)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO profiles (name, username, password, venue_prefs, time_prefs, schedule_weekdays)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, username, password, venue_prefs, time_prefs, schedule_weekdays),
        )
        await db.commit()
        return await get_profile(cursor.lastrowid)


async def clone_profile(profile_id: int, name: str | None = None) -> dict | None:
    """Copy profile configuration only; runtime state and latest result are reset."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT name, username, password, target_days, venue_prefs, time_prefs,
                   schedule_weekdays, schedule_time, schedule_mode, pre_query_delay,
                   max_bookings, group_name, booking_channel, dual_slot_enabled, dual_slot_prefs
            FROM profiles
            WHERE id = ?
            """,
            (profile_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        clone_name = name or f"{row[0]} 副本"
        insert_cursor = await db.execute(
            """
            INSERT INTO profiles (
                name, username, password, target_days, venue_prefs, time_prefs,
                schedule_enabled, schedule_weekdays, schedule_time, schedule_mode,
                pre_query_delay, max_bookings, group_name, booking_channel,
                dual_slot_enabled, dual_slot_prefs, status,
                latest_booking_result, latest_booking_result_at, latest_booking_result_unread
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', '', NULL, 0)
            """,
            (
                clone_name,
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                row[10],
                row[11],
                row[12] if len(row) > 12 else "8080",
                row[13] if len(row) > 13 else 0,
                row[14] if len(row) > 14 else "[]",
            ),
        )
        await db.commit()
        return await get_profile(insert_cursor.lastrowid)


async def update_profile(profile_id: int, updates: dict) -> dict | None:
    if not updates:
        return await get_profile(profile_id)
    # Serialize JSON fields
    for key in ("target_days", "venue_prefs", "time_prefs", "schedule_weekdays", "dual_slot_prefs"):
        if key in updates and not isinstance(updates[key], str):
            updates[key] = json.dumps(updates[key], ensure_ascii=False)
    if "schedule_enabled" in updates:
        updates["schedule_enabled"] = int(updates["schedule_enabled"])
    if "dual_slot_enabled" in updates:
        updates["dual_slot_enabled"] = int(updates["dual_slot_enabled"])

    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [profile_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE profiles SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
            vals,
        )
        await db.commit()
    return await get_profile(profile_id)


async def delete_profile(profile_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()
        return cursor.rowcount > 0


async def update_status(profile_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE profiles SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (status, profile_id),
        )
        await db.commit()


async def record_booking_result(profile_id: int, booking_date: str, result: str, unread: bool = True):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO booking_day_statuses (profile_id, booking_date, result, updated_at)
            VALUES (?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(profile_id, booking_date) DO UPDATE SET
                result = excluded.result,
                updated_at = datetime('now','localtime')
            """,
            (profile_id, booking_date, result),
        )
        await db.execute(
            """
            UPDATE profiles
            SET latest_booking_result = ?,
                latest_booking_result_at = datetime('now','localtime'),
                latest_booking_result_unread = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (result, int(unread), profile_id),
        )
        await db.commit()


async def list_booking_day_statuses(profile_id: int, booking_dates: list[str]) -> dict[str, str]:
    if not booking_dates:
        return {}
    placeholders = ",".join("?" for _ in booking_dates)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"""
            SELECT booking_date, result
            FROM booking_day_statuses
            WHERE profile_id = ? AND booking_date IN ({placeholders})
            """,
            [profile_id, *booking_dates],
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}


async def update_latest_booking_result(profile_id: int, result: str, unread: bool = True):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE profiles
            SET latest_booking_result = ?,
                latest_booking_result_at = datetime('now','localtime'),
                latest_booking_result_unread = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (result, int(unread), profile_id),
        )
        await db.commit()


async def clear_latest_booking_result(profile_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE profiles
            SET latest_booking_result_unread = 0,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (profile_id,),
        )
        await db.commit()

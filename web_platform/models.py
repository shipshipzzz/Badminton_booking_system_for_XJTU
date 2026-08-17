"""Pydantic models for API request/response validation."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# ==================== Venue / Time Preferences ====================

class VenuePreference(BaseModel):
    id: int
    name: str
    enabled: bool = True
    courts: list[int] = []
    courtPriority: dict[str, int] = {}


class TimePreference(BaseModel):
    time: str
    enabled: bool = False
    fanout: int = Field(default=4, ge=0, le=10)
    parallel: int = Field(default=1, ge=1, le=10)
    priority: int = Field(default=1, ge=1, le=5)


# ==================== Profile CRUD ====================

VENUES = [
    {"id": 101, "name": "一号巨构", "totalCourts": 3},
    {"id": 103, "name": "二号巨构", "totalCourts": 6},
    {"id": 104, "name": "三号巨构", "totalCourts": 3},
]


def default_venue_prefs() -> list[dict]:
    return [
        {"id": 103, "name": "二号巨构", "enabled": True,
         "courts": [1, 2, 3, 4, 5, 6],
         "courtPriority": {str(c): c for c in range(1, 7)}},
        {"id": 101, "name": "一号巨构", "enabled": True,
         "courts": [1, 2, 3],
         "courtPriority": {str(c): c for c in range(1, 4)}},
        {"id": 104, "name": "三号巨构", "enabled": True,
         "courts": [1, 2, 3],
         "courtPriority": {str(c): c for c in range(1, 4)}},
    ]


def default_time_prefs() -> list[dict]:
    slots = []
    time_slots = [
        "08:00-09:00",
        "09:01-10:00",
        "10:01-11:00",
        "11:01-12:00",
        "14:30-15:30",
        "15:31-16:30",
        "16:31-17:30",
        "17:31-18:30",
        "18:31-19:30",
        "19:31-20:30",
        "20:31-21:30",
    ]
    for i, time_str in enumerate(time_slots):
        slots.append({
            "time": time_str,
            "enabled": i < 2,
            "fanout": 4,
            "parallel": 1,
            "priority": 1,
        })
    return slots


class ProfileCreate(BaseModel):
    name: str = "New Profile"
    username: str = ""
    password: str = ""
    group_name: str = ""


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    group_name: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    target_days: Optional[list[int]] = None
    venue_prefs: Optional[list[VenuePreference]] = None
    time_prefs: Optional[list[TimePreference]] = None
    schedule_enabled: Optional[bool] = None
    schedule_weekdays: Optional[list[int]] = None
    schedule_time: Optional[str] = None
    schedule_mode: Optional[str] = None
    pre_query_delay: Optional[int] = None
    max_bookings: Optional[int] = None
    booking_channel: Optional[str] = None


class ProfileResponse(BaseModel):
    id: int
    name: str
    group_name: str = ""
    username: str
    password: str
    target_days: list[int]
    venue_prefs: list[dict]
    time_prefs: list[dict]
    schedule_enabled: bool
    schedule_weekdays: list[int]
    schedule_time: str
    schedule_mode: str
    pre_query_delay: int
    max_bookings: int
    booking_channel: str = "8080"
    status: str
    next_schedule_at: Optional[str] = None
    latest_booking_result: str = ""
    latest_booking_result_at: Optional[str] = None
    latest_booking_result_unread: bool = False
    created_at: str
    updated_at: str


class ProfileSummary(BaseModel):
    id: int
    name: str
    group_name: str = ""
    username: str
    booking_channel: str = "8080"
    status: str
    next_schedule_at: Optional[str] = None
    latest_booking_result: str = ""
    latest_booking_result_at: Optional[str] = None
    latest_booking_result_unread: bool = False


class StartRequest(BaseModel):
    immediate: bool = False

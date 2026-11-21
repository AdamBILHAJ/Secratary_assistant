"""Classroom availability and AI recommendation endpoint."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.classroom_availability import (
    DEFAULT_WEEK_START,
    calculate_active_week,
    compute_availability,
    default_room_catalogue,
    load_room_capacities,
    normalize_day,
    parse_time_slot,
)
from services.gemini_service import generate_ai_advice
from services.schedule_store import (
    ScheduleStoreError,
    count_schedules,
    distinct_rooms,
    fetch_occupancies,
    get_week_start,
)

router = APIRouter(tags=["classrooms"])


class ClassroomCheckRequest(BaseModel):
    day: str = Field(
        ...,
        description="Day of the week (lundi to samedi).",
        examples=["lundi"],
    )
    time_slot: str = Field(
        ...,
        description="Time range, e.g. 08:30-10:00 or 8h30-10h00.",
        examples=["08:30-10:00"],
    )
    capacity_needed: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="Optional number of seats required.",
        examples=[30],
    )
    target_date: date | None = Field(
        default=None,
        description=(
            "Date used to determine the active alternating week (Q1/Q2). "
            "Defaults to the server's current date."
        ),
        examples=["2026-09-07"],
    )


class AvailableRoom(BaseModel):
    room: str
    capacity: int | None = None
    capacity_known: bool = False
    suitable_for_capacity: bool = True


class Occupancy(BaseModel):
    room: str
    class_name: str | None = None
    subject: str | None = None
    teacher: str | None = None
    week: str | None = None
    group: str | None = None
    time_slot: str | None = None


class AIAdviceResponse(BaseModel):
    summary: str
    recommendation: str
    recommended_room: str | None = None
    source: Literal["gemini", "fallback"]
    model: str | None = None
    error: str | None = None


class ClassroomCheckResponse(BaseModel):
    day: str
    time_slot: str
    requested_slots: list[str]
    target_date: date
    week_start: str
    active_week: Literal["Q1", "Q2"]
    capacity_needed: int | None = None
    total_rooms: int
    available_count: int
    available_rooms: list[AvailableRoom]
    available_room_names: list[str]
    suitable_room_names: list[str]
    occupied_rooms: list[str]
    occupancies: list[Occupancy]
    source: str = "mongodb"
    ai: AIAdviceResponse


@router.post(
    "/check",
    response_model=ClassroomCheckResponse,
    summary="Check classroom availability for a day and time slot",
)
async def check_classroom_availability(
    payload: ClassroomCheckRequest,
) -> ClassroomCheckResponse:
    """Return free/occupied rooms for the active week plus an AI recommendation."""
    try:
        day = normalize_day(payload.day)
        slot = parse_time_slot(payload.time_slot)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        stored_count = await count_schedules()
    except ScheduleStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"MongoDB Atlas is unavailable. Check MONGODB_URI: {exc}",
        ) from exc

    if stored_count == 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "No schedules are stored yet. Run POST /api/schedules/sync first, "
                "then retry the availability check."
            ),
        )

    try:
        stored_week_start = await get_week_start()
    except ScheduleStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not query the schedule week start: {exc}",
        ) from exc

    week_start = stored_week_start or DEFAULT_WEEK_START
    target_date = payload.target_date or date.today()
    try:
        active_week = calculate_active_week(target_date, week_start)
    except ValueError:
        # A malformed legacy week_start must not make the endpoint unusable.
        week_start = DEFAULT_WEEK_START
        active_week = calculate_active_week(target_date, week_start)

    try:
        occupancies = await fetch_occupancies(day, slot["slots"], active_week)
        stored_rooms = await distinct_rooms()
    except ScheduleStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not query MongoDB schedules: {exc}",
        ) from exc

    capacities = load_room_capacities()
    known_rooms = set(default_room_catalogue()) | set(stored_rooms)
    availability = compute_availability(
        known_rooms=known_rooms,
        occupancies=occupancies,
        capacity_needed=payload.capacity_needed,
        capacities=capacities,
    )

    ai_advice = await generate_ai_advice(
        day=day,
        time_slot=slot["label"],
        capacity_needed=payload.capacity_needed,
        available_rooms=availability["available_rooms"],
        occupied_rooms=availability["occupied_rooms"],
        occupancies=availability["occupancies"],
        total_rooms=availability["total_rooms"],
        target_date=target_date.isoformat(),
        week_start=week_start,
        active_week=active_week,
    )

    result: dict[str, Any] = {
        "day": day,
        "time_slot": slot["label"],
        "requested_slots": slot["slots"],
        "target_date": target_date,
        "week_start": week_start,
        "active_week": active_week,
        "capacity_needed": payload.capacity_needed,
        "total_rooms": availability["total_rooms"],
        "available_count": len(availability["available_rooms"]),
        "available_rooms": availability["available_rooms"],
        "available_room_names": availability["available_room_names"],
        "suitable_room_names": availability["suitable_room_names"],
        "occupied_rooms": availability["occupied_rooms"],
        "occupancies": availability["occupancies"],
        "source": "mongodb",
        "ai": ai_advice,
    }
    return ClassroomCheckResponse.model_validate(result)

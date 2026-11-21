"""Pure availability helpers for the classroom recommendation endpoint."""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import date
from typing import Any

from services.timetable_parser import DAYS, DEFAULT_ROOMS, TIME_SLOTS

logger = logging.getLogger(__name__)

# Week 0 starts on this date and is Q1; every following 7-day block alternates
# Q1 -> Q2 -> Q1. The parser also stores this value on each schedule entry.
DEFAULT_WEEK_START = "2026-09-07"

DAY_ALIASES = {
    "lun": "lundi",
    "monday": "lundi",
    "mon": "lundi",
    "mar": "mardi",
    "tuesday": "mardi",
    "tue": "mardi",
    "mer": "mercredi",
    "wednesday": "mercredi",
    "wed": "mercredi",
    "jeu": "jeudi",
    "thursday": "jeudi",
    "thu": "jeudi",
    "ven": "vendredi",
    "friday": "vendredi",
    "fri": "vendredi",
    "sam": "samedi",
    "saturday": "samedi",
    "sat": "samedi",
}

_TIME_RE = re.compile(
    r"^\s*(\d{1,2})\s*[h:]\s*(\d{2})\s*[-–—]\s*(\d{1,2})\s*[h:]\s*(\d{2})\s*$"
)


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_day(value: str) -> str:
    """Normalize 'Lundi', 'lun.', 'Monday' to the PDF day key."""
    cleaned = _strip_accents(value or "").lower().strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = DAY_ALIASES.get(cleaned, cleaned)
    if cleaned not in DAYS:
        raise ValueError(
            f"Unknown day '{value}'. Expected one of: {', '.join(DAYS)}."
        )
    return cleaned


def _to_minutes(hours: str, minutes: str) -> int:
    hour = int(hours)
    minute = int(minutes)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Invalid time value")
    return hour * 60 + minute


def _format_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_time_slot(value: str) -> dict[str, Any]:
    """Parse a time range and return overlapping canonical slots.

    Accepts ``08:30-10:00``, ``8h30-10h00`` and en/em dash variants.
    """
    match = _TIME_RE.match(value or "")
    if not match:
        raise ValueError(
            "Invalid time_slot. Use a format such as '08:30-10:00' or '8h30-10h00'."
        )

    start = _to_minutes(match.group(1), match.group(2))
    end = _to_minutes(match.group(3), match.group(4))
    if end <= start:
        raise ValueError("time_slot end must be later than its start")

    timetable_start = _to_minutes("08", "30")
    timetable_end = _to_minutes("18", "30")
    if start < timetable_start or end > timetable_end:
        raise ValueError("time_slot must fall between 08:30 and 18:30")

    target_slots: list[str] = []
    for slot in TIME_SLOTS:
        slot_start = _to_minutes(*slot["start"].split(":"))
        slot_end = _to_minutes(*slot["end"].split(":"))
        if max(start, slot_start) < min(end, slot_end):
            target_slots.append(slot["label"])

    if not target_slots:
        raise ValueError("time_slot does not overlap any timetable period")

    return {
        "label": f"{_format_minutes(start)}-{_format_minutes(end)}",
        "start_time": _format_minutes(start),
        "end_time": _format_minutes(end),
        "slots": target_slots,
    }


def calculate_active_week(
    target_date: date | str,
    week_start: str | date = DEFAULT_WEEK_START,
) -> str:
    """Return the active alternating week (``Q1`` or ``Q2``) for a date.

    Week 0 begins at ``week_start`` and is Q1. Weeks alternate every seven
    days: ``weeks_elapsed = (target_date - week_start).days // 7``.
    """
    try:
        target = (
            target_date
            if isinstance(target_date, date)
            else date.fromisoformat(str(target_date))
        )
        start = (
            week_start
            if isinstance(week_start, date)
            else date.fromisoformat(str(week_start))
        )
    except ValueError as exc:
        raise ValueError(
            "Invalid date for week calculation; expected YYYY-MM-DD."
        ) from exc

    weeks_elapsed = (target - start).days // 7
    return "Q1" if weeks_elapsed % 2 == 0 else "Q2"


def load_room_capacities() -> dict[str, int]:
    """Read optional room capacities from ``ROOM_CAPACITIES_JSON``.

    Example: ``ROOM_CAPACITIES_JSON={"I6": 30, "C9": 40}``.
    """
    raw = os.getenv("ROOM_CAPACITIES_JSON", "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Ignoring invalid ROOM_CAPACITIES_JSON: %s", exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("ROOM_CAPACITIES_JSON must be a JSON object")
        return {}

    capacities: dict[str, int] = {}
    for room, capacity in data.items():
        if isinstance(capacity, int) and not isinstance(capacity, bool) and capacity > 0:
            capacities[str(room).strip().upper()] = capacity
    return capacities


def _room_sort_key(room: str) -> tuple[str, int, str]:
    match = re.match(r"^([A-Za-z]*)(\d*)", room)
    if not match:
        return (room, 0, room)
    return (match.group(1).upper(), int(match.group(2) or 0), room)


def compute_availability(
    known_rooms: list[str] | set[str],
    occupancies: list[dict[str, Any]],
    capacity_needed: int | None = None,
    capacities: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Compare known rooms with occupied rooms and annotate capacity fit."""
    capacities = capacities or {}
    occupied_map: dict[str, list[dict[str, Any]]] = {}

    for item in occupancies:
        room = str(item.get("room") or "").strip().upper()
        if not room or room in {"Q1", "Q2"}:
            continue
        occupied_map.setdefault(room, []).append(
            {
                "class_name": item.get("class_name"),
                "subject": item.get("subject"),
                "teacher": item.get("teacher"),
                "week": item.get("week"),
                "group": item.get("group"),
                "time_slot": item.get("time_slot"),
            }
        )

    all_rooms = {
        str(room).strip().upper()
        for room in known_rooms
        if str(room).strip() and str(room).strip().upper() not in {"Q1", "Q2"}
    }
    all_rooms.update(occupied_map)
    available_rooms = sorted(all_rooms - set(occupied_map), key=_room_sort_key)

    room_details: list[dict[str, Any]] = []
    for room in available_rooms:
        capacity = capacities.get(room)
        capacity_known = capacity is not None
        suitable = (
            capacity_needed is None
            or not capacity_known
            or (capacity is not None and capacity >= capacity_needed)
        )
        room_details.append(
            {
                "room": room,
                "capacity": capacity,
                "capacity_known": capacity_known,
                "suitable_for_capacity": suitable,
            }
        )

    suitable_rooms = [room["room"] for room in room_details if room["suitable_for_capacity"]]

    return {
        "rooms_considered": sorted(all_rooms, key=_room_sort_key),
        "total_rooms": len(all_rooms),
        "available_rooms": room_details,
        "available_room_names": available_rooms,
        "suitable_room_names": suitable_rooms,
        "occupied_rooms": sorted(occupied_map, key=_room_sort_key),
        "occupancies": sorted(
            [
                {
                    "room": room,
                    **occupancy,
                }
                for room, items in occupied_map.items()
                for occupancy in items
            ],
            key=lambda item: (_room_sort_key(item["room"]), str(item.get("time_slot") or "")),
        ),
    }


def default_room_catalogue() -> list[str]:
    """Return the built-in room list used when MongoDB has no rooms yet."""
    return list(DEFAULT_ROOMS)

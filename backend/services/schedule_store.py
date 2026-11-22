"""MongoDB persistence for parsed timetable entries."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from pymongo import ReplaceOne
from pymongo.errors import PyMongoError

from database import get_collection

logger = logging.getLogger(__name__)

SCHEDULES_COLLECTION = "schedules"


class ScheduleStoreError(RuntimeError):
    """Raised when schedules cannot be stored or queried."""


def flatten_entries(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``classes[].entries[]`` into one list of course entries."""
    entries: list[dict[str, Any]] = []
    for class_schedule in parsed.get("classes", []):
        entries.extend(class_schedule.get("entries", []))
    return entries


async def _ensure_indexes(collection) -> None:
    try:
        await collection.create_index("entry_key", unique=True)
        await collection.create_index([("day", 1), ("slots", 1), ("week", 1)])
        await collection.create_index("room")
        await collection.create_index([("class_name", 1), ("day", 1)])
    except (PyMongoError, RuntimeError) as exc:
        # Duplicate legacy documents must not block an otherwise valid sync.
        logger.warning("Could not create schedules indexes: %s", exc)


async def upsert_schedules(parsed: dict[str, Any]) -> dict[str, Any]:
    """Idempotently upsert all entries and remove stale rows for this source."""
    entries = flatten_entries(parsed)
    if not entries:
        raise ScheduleStoreError("the parsed timetable contains no entries")

    source_file = parsed.get("source_file", "timetable.pdf")
    imported_at = datetime.now(UTC)

    try:
        collection = get_collection(SCHEDULES_COLLECTION)
        await _ensure_indexes(collection)

        operations = [
            ReplaceOne(
                {"entry_key": entry["entry_key"]},
                {**entry, "imported_at": imported_at},
                upsert=True,
            )
            for entry in entries
        ]
        result = await collection.bulk_write(operations, ordered=False)

        keys = [entry["entry_key"] for entry in entries]
        deleted = await collection.delete_many(
            {"source_file": source_file, "entry_key": {"$nin": keys}}
        )
    except (PyMongoError, RuntimeError) as exc:
        raise ScheduleStoreError(f"MongoDB schedules upsert failed: {exc}") from exc

    return {
        "collection": SCHEDULES_COLLECTION,
        "source_file": source_file,
        "entry_count": len(entries),
        "matched": result.matched_count,
        "modified": result.modified_count,
        "upserted": result.upserted_count,
        "deleted": deleted.deleted_count,
        "synced_at": imported_at.isoformat(),
    }


async def count_schedules() -> int:
    """Return the number of stored course entries."""
    try:
        return await get_collection(SCHEDULES_COLLECTION).count_documents({})
    except (PyMongoError, RuntimeError) as exc:
        raise ScheduleStoreError(f"MongoDB schedule count failed: {exc}") from exc


async def fetch_occupancies(
    day: str, slots: list[str], active_week: str | None = None
) -> list[dict[str, Any]]:
    """Return entries whose time slots overlap the requested slots.

    When ``active_week`` is supplied, entries limited to the other alternating
    week are excluded. Entries without a week (full-class sessions) always
    count as occupied.
    """
    if not slots:
        return []

    query: dict[str, Any] = {"day": day, "slots": {"$in": slots}}
    if active_week:
        query["$or"] = [
            {"week": active_week},
            {"week": None},
            {"week": {"$exists": False}},
        ]

    try:
        cursor = get_collection(SCHEDULES_COLLECTION).find(
            query,
            {"_id": 0, "imported_at": 0},
        )
        documents = await cursor.to_list(length=2000)
    except (PyMongoError, RuntimeError) as exc:
        raise ScheduleStoreError(f"MongoDB schedule query failed: {exc}") from exc

    documents.sort(
        key=lambda item: (
            str(item.get("room") or ""),
            str(item.get("time_slot") or ""),
            str(item.get("class_name") or ""),
        )
    )
    return documents


async def fetch_class_schedules(
    class_name: str, limit: int = 500
) -> list[dict[str, Any]]:
    """Return stored schedule entries for one class, sorted for documents.

    The query is read-only and matches the class name case-insensitively, so a
    document can be enriched without mutating any stored schedule data.
    """
    normalized = (class_name or "").strip()
    if not normalized:
        return []

    query = {
        "class_name": {
            "$regex": f"^{re.escape(normalized)}$",
            "$options": "i",
        }
    }
    try:
        cursor = (
            get_collection(SCHEDULES_COLLECTION)
            .find(query, {"_id": 0, "imported_at": 0})
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
    except (PyMongoError, RuntimeError) as exc:
        raise ScheduleStoreError(f"MongoDB class schedule query failed: {exc}") from exc

    documents.sort(
        key=lambda item: (
            str(item.get("day") or ""),
            str(item.get("start_time") or item.get("time_slot") or ""),
            str(item.get("subject") or ""),
        )
    )
    return documents


async def get_week_start() -> str | None:
    """Return the ISO week start stored with the most recent timetable sync."""
    try:
        cursor = (
            get_collection(SCHEDULES_COLLECTION)
            .find(
                {"week_start": {"$ne": None}},
                {"_id": 0, "week_start": 1, "imported_at": 1},
            )
            .sort("imported_at", -1)
            .limit(1)
        )
        documents = await cursor.to_list(length=1)
    except (PyMongoError, RuntimeError) as exc:
        raise ScheduleStoreError(f"MongoDB week-start lookup failed: {exc}") from exc

    if not documents:
        return None
    value = documents[0].get("week_start")
    return str(value) if value else None


async def distinct_rooms() -> list[str]:
    """Return every room code present in the schedules collection."""
    try:
        values = await get_collection(SCHEDULES_COLLECTION).distinct("room")
    except (PyMongoError, RuntimeError) as exc:
        raise ScheduleStoreError(f"MongoDB room lookup failed: {exc}") from exc
    return sorted(
        {
            str(value).strip()
            for value in values
            if value and str(value).strip().upper() not in {"Q1", "Q2"}
        }
    )

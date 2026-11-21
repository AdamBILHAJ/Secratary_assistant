"""Schedule parsing and MongoDB synchronisation endpoints."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from services.schedule_store import (
    ScheduleStoreError,
    count_schedules,
    distinct_rooms,
    get_week_start,
    upsert_schedules,
)
from services.timetable_parser import (
    TimetableFileNotFound,
    TimetableParseError,
    parse_timetable_pdf,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["schedules"])


@router.get("/debug-parse", summary="Parse the timetable PDF without storing it")
async def debug_parse() -> dict[str, Any]:
    """Return the raw structured JSON produced by the PDF parser.

    This endpoint does not touch MongoDB, so it can be used to inspect the
    exact data shape before running a sync.
    """
    try:
        parsed = await asyncio.to_thread(parse_timetable_pdf)
    except TimetableFileNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimetableParseError as exc:
        raise HTTPException(
            status_code=422, detail=f"Could not parse timetable PDF: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected timetable parse failure")
        raise HTTPException(
            status_code=500, detail=f"Unexpected parser failure: {exc}"
        ) from exc

    return parsed


@router.post("/sync", summary="Parse the PDF and upsert schedules into MongoDB")
async def sync_schedules() -> dict[str, Any]:
    """Parse the PDF on demand and upsert every entry into ``schedules``."""
    try:
        parsed = await asyncio.to_thread(parse_timetable_pdf)
    except TimetableFileNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimetableParseError as exc:
        raise HTTPException(
            status_code=422, detail=f"Could not parse timetable PDF: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected timetable parse failure during sync")
        raise HTTPException(
            status_code=500, detail=f"Unexpected parser failure: {exc}"
        ) from exc

    try:
        storage = await upsert_schedules(parsed)
    except ScheduleStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Schedules could not be stored. Check MongoDB Atlas: {exc}",
        ) from exc

    return {
        "status": "success",
        "message": "Timetable parsed and synchronised",
        "source_file": parsed["source_file"],
        "week_start": parsed["week_start"],
        "class_count": parsed["class_count"],
        "entry_count": parsed["entry_count"],
        "warnings": parsed["warnings"],
        "storage": storage,
    }


@router.get("/status", summary="Check how many schedules are stored")
async def schedules_status() -> dict[str, Any]:
    """Return a small summary of the MongoDB ``schedules`` collection."""
    try:
        entry_count = await count_schedules()
        rooms = await distinct_rooms()
        week_start = await get_week_start()
    except ScheduleStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"MongoDB schedules are unavailable: {exc}",
        ) from exc

    return {
        "collection": "schedules",
        "entry_count": entry_count,
        "week_start": week_start,
        "rooms": rooms,
        "ready": entry_count > 0,
    }

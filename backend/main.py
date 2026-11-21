"""FastAPI entry point for the IT Department Automation App."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from database import close_database, ping_database
from routes import classrooms, schedules
from services.schedule_store import upsert_schedules
from services.timetable_parser import parse_timetable_pdf

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

APP_NAME = "IT Department Automation API"
APP_VERSION = "0.3.0"
STARTED_AT = time.time()

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Warm up Atlas, optionally sync schedules, then release the client."""
    result = await ping_database()
    if result["status"] == "connected":
        logger.info("MongoDB Atlas connected (%s ms)", result["latency_ms"])
    else:
        logger.warning(
            "MongoDB Atlas unavailable at startup: %s", result.get("error", "unknown")
        )

    if _env_flag("AUTO_SYNC_SCHEDULES") and result["status"] == "connected":
        try:
            parsed = await asyncio.to_thread(parse_timetable_pdf)
            storage = await upsert_schedules(parsed)
            logger.info(
                "Auto-synced %s schedule entries from %s",
                storage["entry_count"],
                parsed["source_file"],
            )
        except Exception as exc:
            logger.warning("Schedule auto-sync skipped: %s", exc)
    elif _env_flag("AUTO_SYNC_SCHEDULES"):
        logger.warning("Schedule auto-sync skipped: MongoDB is not connected")

    yield
    await close_database()


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="REST API for the IT Department Automation App.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(schedules.router, prefix="/api/schedules")
app.include_router(classrooms.router, prefix="/api/classrooms")


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": APP_NAME,
        "docs": "/docs",
        "health": "/api/health",
        "schedules_debug": "/api/schedules/debug-parse",
        "classroom_check": "/api/classrooms/check",
    }


@app.get("/api/health", tags=["system"])
async def health_check() -> JSONResponse:
    """Health check for the API server and the MongoDB Atlas connection."""
    mongodb = await ping_database()
    is_healthy = mongodb["status"] == "connected"

    return JSONResponse(
        status_code=200 if is_healthy else 503,
        content={
            "status": "ok" if is_healthy else "degraded",
            "service": APP_NAME,
            "version": APP_VERSION,
            "timestamp": datetime.now(UTC).isoformat(),
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "dependencies": {"mongodb": mongodb},
        },
    )


# Unlisted compatibility alias for external probes, browser checks and legacy
# clients. It intentionally delegates to the canonical handler above.
app.add_api_route(
    "/health",
    health_check,
    methods=["GET"],
    include_in_schema=False,
)

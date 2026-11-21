"""MongoDB Atlas connection setup for the IT Department Automation App.

Uses PyMongo's native async API (``AsyncMongoClient``), which is the
successor to the now-deprecated Motor driver. The client is created lazily
and reused for the lifetime of the process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

# Load backend/.env regardless of the directory uvicorn is launched from.
load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)

MONGODB_DB = os.getenv("MONGODB_DB", "it_automation").strip() or "it_automation"

# Fail fast instead of hanging for 30s when Atlas is unreachable.
try:
    SERVER_SELECTION_TIMEOUT_MS = int(os.getenv("MONGODB_TIMEOUT_MS", "5000"))
except ValueError:
    logger.warning("Invalid MONGODB_TIMEOUT_MS; using 5000 ms")
    SERVER_SELECTION_TIMEOUT_MS = 5000

_client: AsyncMongoClient | None = None


def _get_uri() -> str:
    """Return the configured Atlas URI or raise a helpful error."""
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set. Copy backend/.env.example to backend/.env "
            "and add your MongoDB Atlas connection string."
        )
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise RuntimeError(
            "MONGODB_URI must start with 'mongodb://' or 'mongodb+srv://'."
        )
    return uri


def get_client() -> AsyncMongoClient:
    """Return the shared async Mongo client, creating it on first use."""
    global _client
    if _client is None:
        _client = AsyncMongoClient(
            _get_uri(),
            appname="it-department-automation",
            serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
            connectTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
            tz_aware=True,
        )
        logger.info("Created MongoDB client for database %r", MONGODB_DB)
    return _client


def get_database() -> AsyncDatabase:
    """Return the application database handle."""
    return get_client()[MONGODB_DB]


def get_collection(name: str):
    """Convenience helper for accessing a collection."""
    return get_database()[name]


async def ping_database() -> dict[str, Any]:
    """Run an Atlas ``ping`` command and report the result.

    Never raises: connection problems are returned as a status payload so
    the health endpoint can respond with a useful 503 instead of crashing.
    """
    started = time.perf_counter()
    try:
        await get_client().admin.command("ping")
        return {
            "status": "connected",
            "database": MONGODB_DB,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:  # health endpoint must never crash
        logger.warning("MongoDB ping failed: %s", exc)
        return {
            "status": "disconnected",
            "database": MONGODB_DB,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc),
        }


async def close_database() -> None:
    """Close the shared client (call on application shutdown)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("MongoDB client closed")


if __name__ == "__main__":
    # Smoke test: python database.py
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(ping_database()))

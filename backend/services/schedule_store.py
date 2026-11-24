"""MongoDB persistence for parsed timetable entries."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any

from pymongo import ReplaceOne
from pymongo.errors import PyMongoError

from database import get_collection

logger = logging.getLogger(__name__)

SCHEDULES_COLLECTION = "schedules"

# Optional JSON object of extra aliases, e.g.
# ``{"bd": ["BD", "Base de Données"], "l2 rsi": ["L2-RSI", "L2 RSI 2"]}``.
SCHEDULE_ALIASES_ENV = "SCHEDULE_ALIASES_JSON"
SCHEDULE_VOCAB_TTL_ENV = "SCHEDULE_VOCAB_TTL_SECONDS"

# Each group is an allowlist: only these stored fields may be queried for the
# corresponding router filter. Nothing outside this list reaches MongoDB.
CLASS_FIELDS = (
    "class_name",
    "class",
    "level",
    "group",
    "group_name",
    "track",
    "specialty",
    "section",
)
SUBJECT_FIELDS = (
    "subject",
    "module",
    "module_name",
    "title",
    "course",
    "course_name",
    "ue",
    "matiere",
)
TEACHER_FIELDS = ("teacher", "professor", "instructor", "enseignant")
ROOM_FIELDS = ("room", "salle", "classroom")
DAY_FIELDS = ("day", "jour")
WEEK_FIELDS = ("week", "group", "badge")

VOCABULARY_FIELDS: dict[str, tuple[str, ...]] = {
    "class_name": CLASS_FIELDS,
    "subject": SUBJECT_FIELDS,
    "teacher": TEACHER_FIELDS,
    "room": ROOM_FIELDS,
    "week": WEEK_FIELDS,
}

_BASE_ALIASES: dict[str, tuple[str, ...]] = {
    "bd": ("BD", "BDD", "DB", "SGBD", "Base de Données", "Database"),
    "bdd": ("BD", "BDD", "DB", "SGBD", "Base de Données", "Database"),
    "db": ("BD", "BDD", "DB", "SGBD", "Base de Données", "Database"),
    "sgbd": ("BD", "BDD", "DB", "SGBD", "Base de Données", "Database"),
    "base de donnees": ("BD", "BDD", "DB", "SGBD", "Base de Données", "Database"),
    "database": ("BD", "BDD", "DB", "SGBD", "Base de Données", "Database"),
    "rsi": ("RSI", "L2 RSI", "L2 RSI 2", "L2-RSI", "L2-RSI-2", "L2RSI2"),
    "l2 rsi": ("L2-RSI", "L2 RSI", "L2 RSI 2", "L2-RSI-2", "L2RSI2"),
    "l2 rsi 2": ("L2-RSI", "L2 RSI", "L2 RSI 2", "L2-RSI-2", "L2RSI2"),
    "l2-rsi": ("L2-RSI", "L2 RSI", "L2 RSI 2", "L2-RSI-2", "L2RSI2"),
    "l2_rsi_2": ("L2-RSI", "L2 RSI", "L2 RSI 2", "L2-RSI-2", "L2RSI2"),
}

_SEPARATOR_RE = re.compile(r"[\s\-_/\\.]+")
_ACCENT_EQUIVALENTS = {
    "a": "aàáâäãå",
    "c": "cç",
    "e": "eèéêë",
    "i": "iìíîï",
    "n": "nñ",
    "o": "oòóôöõ",
    "u": "uùúûü",
    "y": "yÿ",
}

_vocabulary_cache: dict[str, Any] = {"value": None, "expires_at": 0.0}


def normalize_compare(value: Any) -> str:
    """Lowercase, strip accents and unify separators for tolerant comparison."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    return re.sub(r"\s+", " ", _SEPARATOR_RE.sub(" ", text)).strip()


def alias_dictionary() -> dict[str, tuple[str, ...]]:
    """Return built-in aliases merged with optional JSON configuration."""
    aliases = {
        normalize_compare(key): tuple(values)
        for key, values in _BASE_ALIASES.items()
    }
    raw = os.getenv(SCHEDULE_ALIASES_ENV, "").strip()
    if not raw:
        return aliases
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Ignoring invalid %s: %s", SCHEDULE_ALIASES_ENV, exc)
        return aliases
    if not isinstance(configured, dict):
        logger.warning("%s must be a JSON object", SCHEDULE_ALIASES_ENV)
        return aliases

    for key, values in configured.items():
        if isinstance(values, str):
            extra = (values,)
        elif isinstance(values, (list, tuple)):
            extra = tuple(str(value) for value in values)
        else:
            continue
        normalized_key = normalize_compare(key)
        if normalized_key:
            aliases[normalized_key] = tuple(
                dict.fromkeys([*aliases.get(normalized_key, ()), *extra])
            )
    return aliases


def expand_aliases(value: Any) -> list[str]:
    """Return the normalized value plus any alias/token expansions."""
    normalized = normalize_compare(value)
    if not normalized:
        return []
    aliases = alias_dictionary()
    variants = [normalized]
    variants.extend(
        normalize_compare(alias) for alias in aliases.get(normalized, ())
    )

    tokens = normalized.split()
    for index, token in enumerate(tokens):
        for alias in aliases.get(token, ()):
            expanded = tokens[:index] + normalize_compare(alias).split() + tokens[index + 1 :]
            variants.append(" ".join(expanded))
    return list(dict.fromkeys(variant for variant in variants if variant))


def _letter_pattern(char: str) -> str:
    alternatives = _ACCENT_EQUIVALENTS.get(char.lower(), char.lower())
    return f"[{re.escape(alternatives)}]"


def normalized_value_regex(value: Any, *, anchored: bool = False) -> str:
    """Build an accent/separator tolerant regex for a router filter value."""
    variants = expand_aliases(value)
    if not variants:
        return re.escape(normalize_compare(value))

    separator = r"[\s\-_/]+"
    patterns: list[str] = []
    for variant in variants:
        tokens = variant.split()
        if not tokens:
            continue
        body = separator.join(
            "".join(
                _letter_pattern(char) if char.isalpha() else re.escape(char)
                for char in token
            )
            for token in tokens
        )
        patterns.append(rf"^{body}(?:$|{separator})" if anchored else body)
    return "(?:" + "|".join(patterns or [re.escape(variants[0])]) + ")"


def _multi_field_filter(fields: tuple[str, ...], value: Any, *, anchored: bool) -> dict[str, Any]:
    pattern = normalized_value_regex(value, anchored=anchored)
    return {
        "$or": [
            {field: {"$regex": pattern, "$options": "i"}}
            for field in fields
            if field
        ]
    }


def _build_schedule_query(
    *,
    day: str | None = None,
    slots: list[str] | None = None,
    week: str | None = None,
    room: str | None = None,
    class_name: str | None = None,
    teacher: str | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    """Build a read-only query using only allowlisted schedule fields."""
    conditions: list[dict[str, Any]] = []
    if day:
        conditions.append(_multi_field_filter(DAY_FIELDS, day, anchored=True))
    if slots:
        conditions.append(
            {"slots": {"$in": [str(slot) for slot in slots if str(slot).strip()]}}
        )
    if week:
        conditions.append(
            {"$or": [{"week": week}, {"week": None}, {"week": {"$exists": False}}]}
        )
    if room:
        conditions.append(_multi_field_filter(ROOM_FIELDS, room, anchored=True))
    if class_name:
        conditions.append(_multi_field_filter(CLASS_FIELDS, class_name, anchored=True))
    if teacher:
        conditions.append(_multi_field_filter(TEACHER_FIELDS, teacher, anchored=False))
    if subject:
        conditions.append(_multi_field_filter(SUBJECT_FIELDS, subject, anchored=False))

    if not conditions:
        return {}
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


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


async def query_schedule_entries(
    *,
    day: str | None = None,
    slots: list[str] | None = None,
    week: str | None = None,
    room: str | None = None,
    class_name: str | None = None,
    teacher: str | None = None,
    subject: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Run a read-only, allowlisted filter query against stored schedules.

    Only known schedule fields are queryable and every text value is regex
    escaped, so free-form router output cannot inject MongoDB operators.
    """
    query = _build_schedule_query(
        day=day,
        slots=slots,
        week=week,
        room=room,
        class_name=class_name,
        teacher=teacher,
        subject=subject,
    )

    safe_limit = min(max(int(limit or 500), 1), 1000)
    try:
        cursor = (
            get_collection(SCHEDULES_COLLECTION)
            .find(query, {"_id": 0, "imported_at": 0})
            .limit(safe_limit)
        )
        documents = await cursor.to_list(length=safe_limit)
    except (PyMongoError, RuntimeError) as exc:
        raise ScheduleStoreError(f"MongoDB schedule query failed: {exc}") from exc

    documents.sort(
        key=lambda item: (
            str(item.get("day") or ""),
            str(item.get("start_time") or item.get("time_slot") or ""),
            str(item.get("room") or ""),
            str(item.get("subject") or ""),
        )
    )
    return documents


async def fetch_schedule_vocabulary(
    *,
    force_refresh: bool = False,
    max_values: int = 40,
) -> dict[str, list[str]]:
    """Return cached distinct classes/subjects/teachers/rooms/badges.

    Vocabulary is used only to make routing more accurate. Failures never
    break schedule queries: an empty summary simply means Gemini and the
    deterministic fallbacks rely on alias expansion instead.
    """
    global _vocabulary_cache

    try:
        ttl = float(os.getenv(SCHEDULE_VOCAB_TTL_ENV, "300"))
    except (TypeError, ValueError):
        ttl = 300.0
    ttl = min(max(ttl, 0.0), 3600.0)

    now = time.monotonic()
    cached = _vocabulary_cache.get("value")
    if (
        not force_refresh
        and isinstance(cached, dict)
        and now < float(_vocabulary_cache.get("expires_at", 0.0))
    ):
        return {key: list(values) for key, values in cached.items()}

    values_by_key: dict[str, list[str]] = {}
    try:
        collection = get_collection(SCHEDULES_COLLECTION)
        for key, fields in VOCABULARY_FIELDS.items():
            found: list[str] = []
            for field in fields:
                try:
                    raw_values = await collection.distinct(field)
                except (PyMongoError, RuntimeError) as exc:
                    logger.debug("Could not read distinct %s: %s", field, exc)
                    continue
                for value in raw_values or []:
                    text = str(value).strip()
                    if text:
                        found.append(text)
            values_by_key[key] = sorted(
                dict.fromkeys(found), key=lambda item: normalize_compare(item)
            )[:max_values]
    except (PyMongoError, RuntimeError) as exc:
        logger.warning("Could not load schedule vocabulary: %s", exc)
        return {}

    _vocabulary_cache = {"value": values_by_key, "expires_at": now + ttl}
    return {key: list(values) for key, values in values_by_key.items()}


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

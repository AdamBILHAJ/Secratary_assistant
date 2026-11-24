"""Unified natural-language schedule Q&A routing and deterministic execution.

The router classifies a prompt into one of three actions (room check, free-room
search, or general schedule query), reusing Gemini's rotation/budget helpers.
Every decision is normalized and hard-gated before it can reach MongoDB: low
confidence or a missing room falls back to deterministic parsing, and only
allowlisted schedule fields are queried (read-only).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from datetime import date
from typing import Any

from services import gemini_service
from services.classroom_availability import (
    DEFAULT_WEEK_START,
    calculate_active_week,
    compute_availability,
    default_room_catalogue,
    normalize_day,
    parse_time_slot,
)
from services.gemini_service import ScheduleQueryDecision
from services.schedule_store import (
    ScheduleStoreError,
    distinct_rooms,
    fetch_occupancies,
    query_schedule_entries,
)
from services.timetable_parser import DAYS, TIME_SLOTS

logger = logging.getLogger("services.schedule_query")

_TIME_RANGE_RE = re.compile(
    r"(\d{1,2})\s*[h:]\s*(\d{2})\s*[-–—]\s*(\d{1,2})\s*[h:]\s*(\d{2})"
)
_CLOCK_RE = re.compile(r"\b(\d{1,2})\s*[h:]\s*(\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_FR_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_BADGE_RE = re.compile(r"\b(Q[12]|G[12])\b", re.IGNORECASE)
_CLASS_RE = re.compile(r"\b[A-Z]\d?-[A-Z]{1,4}-\d[A-Z]?\b", re.IGNORECASE)

_WEEK_ALIASES = {
    "q1": "Q1",
    "g1": "Q1",
    "q2": "Q2",
    "g2": "Q2",
}

_FRENCH_MARKERS = (
    "est-ce",
    "est ce",
    "salle",
    "libre",
    "occup",
    "disponible",
    "disponibles",
    "prof",
    "enseignant",
    "cours",
    "matière",
    "emploi",
    "temps",
    "semaine",
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "cherche",
    "trouve",
    "donne",
    "quelles",
    "quels",
    "combien",
)


def _plain(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


def _detect_language(prompt: str) -> str:
    text = _plain(prompt)
    if any(marker in text for marker in _FRENCH_MARKERS):
        return "fr"
    if any(ord(char) > 127 for char in prompt):
        return "fr"
    return "en"


def _extract_date(value: str | None) -> str | None:
    text = str(value or "").strip()
    match = _ISO_DATE_RE.search(text)
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None
    match = _FR_DATE_RE.search(text)
    if match:
        day, month, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


_DAY_ALIASES = {
    **{alias: canonical for alias, canonical in {
        "lun": "lundi", "monday": "lundi", "mon": "lundi", "lundi": "lundi",
        "mar": "mardi", "tuesday": "mardi", "tue": "mardi", "mardi": "mardi",
        "mer": "mercredi", "wednesday": "mercredi", "wed": "mercredi",
        "mercredi": "mercredi", "jeu": "jeudi", "thursday": "jeudi",
        "thu": "jeudi", "jeudi": "jeudi", "ven": "vendredi",
        "friday": "vendredi", "fri": "vendredi", "vendredi": "vendredi",
        "sam": "samedi", "saturday": "samedi", "sat": "samedi",
        "samedi": "samedi",
    }.items()}
}


def _extract_day(value: str | None) -> str | None:
    text = _plain(value)
    if not text:
        return None
    for alias in sorted(_DAY_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text):
            return _DAY_ALIASES[alias]
    return None


def _canonical_time_slot(value: str | None) -> str | None:
    """Validate a router/fallback time slot and return its canonical label."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parse_time_slot(text)["label"]
    except (ValueError, TypeError):
        pass

    clock = _CLOCK_RE.search(text)
    if not clock:
        return None
    try:
        minutes = int(clock.group(1)) * 60 + int(clock.group(2))
    except (TypeError, ValueError):
        return None
    for slot in TIME_SLOTS:
        start_h, start_m = (int(part) for part in slot["start"].split(":"))
        end_h, end_m = (int(part) for part in slot["end"].split(":"))
        if start_h * 60 + start_m <= minutes < end_h * 60 + end_m:
            return slot["label"]
    return None


def _extract_time_slot(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _TIME_RANGE_RE.search(text)
    if match:
        candidate = f"{int(match.group(1)):02d}:{match.group(2)}-{int(match.group(3)):02d}:{match.group(4)}"
        canonical = _canonical_time_slot(candidate)
        if canonical:
            return canonical
    return _canonical_time_slot(text)


def _extract_badge(value: str | None) -> str | None:
    match = _BADGE_RE.search(str(value or ""))
    return _WEEK_ALIASES.get(match.group(1).lower()) if match else None


def _extract_room(value: str | None, known_rooms: list[str] | None = None) -> str | None:
    text = str(value or "")
    candidates = {
        str(room).strip().upper()
        for room in [*(known_rooms or []), *default_room_catalogue()]
        if str(room).strip() and str(room).strip().upper() not in {"Q1", "Q2", "G1", "G2"}
    }
    plain = _plain(text)
    for room in sorted(candidates, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(room.lower())}(?![a-z0-9])", plain):
            return room

    generic = re.search(
        r"(?<![a-z0-9])(?:ti|i|c|s|a)\s?\d{1,3}(?:\([a-z0-9]+\))?(?![a-z0-9])",
        plain,
    )
    if generic:
        return re.sub(r"\s+", "", generic.group(0)).upper()
    return None


def _extract_class_name(value: str | None) -> str | None:
    match = _CLASS_RE.search(str(value or ""))
    return match.group(0).upper() if match else None


_LABEL_STOPWORDS = {
    "a", "an", "and", "dans", "de", "des", "du", "en", "est", "for", "in",
    "le", "les", "on", "pour", "the", "teach", "teaches", "teaching",
    "enseigne", "avec", "with", "q1", "q2", "g1", "g2",
}


def _extract_labelled(value: str | None, labels: tuple[str, ...]) -> str | None:
    text = str(value or "")
    for label in labels:
        match = re.search(
            rf"\b{label}\b\s*[:\-]?\s*([A-Za-zÀ-ÿ][A-Za-z0-9À-ÿ'\- ]{{1,50}})",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        pieces = [
            piece
            for piece in match.group(1).split()
            if piece.strip() and piece.strip().lower() not in _LABEL_STOPWORDS
        ]
        if pieces:
            return " ".join(pieces[:3])[:80]
    return None


def _fallback_schedule_decision(
    prompt: str,
    known_rooms: list[str] | None = None,
    error: str | None = None,
) -> ScheduleQueryDecision:
    """Deterministically classify a prompt when Gemini is unavailable."""
    text = _plain(prompt)
    room = _extract_room(text, known_rooms)
    badge = _extract_badge(text)
    day = _extract_day(text)
    iso_date = _extract_date(text)
    if not day and iso_date:
        try:
            day = DAYS[date.fromisoformat(iso_date).weekday()]
        except ValueError:
            day = None
    time_slot = _extract_time_slot(text)
    class_name = _extract_class_name(text)
    teacher = _extract_labelled(text, ("prof", "professeur", "teacher", "enseignant"))
    subject = _extract_labelled(text, ("subject", "matiere", "matière"))

    check_intent = any(
        marker in text
        for marker in (
            "free",
            "occupied",
            "available room",
            "is room",
            "is the room",
            "libre",
            "occup",
            "disponible",
            "est-ce que la salle",
            "est ce que la salle",
            "la salle est",
        )
    )
    find_intent = any(
        marker in text
        for marker in (
            "find",
            "which rooms",
            "list rooms",
            "free rooms",
            "available rooms",
            "cherche",
            "trouve",
            "salles libres",
            "salles disponibles",
            "donne-moi les salles",
            "donne moi les salles",
        )
    )

    if room and (check_intent or not any((subject, teacher, class_name))):
        action = "check_room_availability"
    elif find_intent or (check_intent and (day or time_slot or badge)):
        action = "find_available_rooms"
    else:
        action = "query_schedule"

    confidence = 0.85
    if room and (day or time_slot or iso_date or badge):
        confidence = 0.92
    elif action == "query_schedule":
        confidence = 0.78

    decision = ScheduleQueryDecision.model_validate(
        {
            "action": action,
            "room": room,
            "day": day,
            "date": iso_date,
            "time_slot": time_slot,
            "week_badge": badge,
            "group": {"Q1": "G1", "Q2": "G2"}.get(badge or ""),
            "class_name": class_name,
            "teacher": teacher,
            "subject": subject,
            "confidence": confidence,
        }
    )
    if error:
        logger.info("Schedule query used deterministic fallback: %s", error)
    return decision


def _normalize_filters(
    decision: ScheduleQueryDecision | dict[str, Any],
    *,
    prompt: str | None = None,
    known_rooms: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve and sanitize every filter before any database access."""
    data = (
        decision.model_dump()
        if isinstance(decision, ScheduleQueryDecision)
        else dict(decision)
    )

    room = str(data.get("room") or "").strip().upper() or None
    badge = _extract_badge(data.get("week_badge") or data.get("group") or "")
    if room in {"Q1", "Q2", "G1", "G2"}:
        badge = badge or _extract_badge(room)
        room = None

    if not room and prompt:
        room = _extract_room(prompt, known_rooms)

    day_value = str(data.get("day") or "").strip()
    try:
        day = normalize_day(day_value) if day_value else None
    except ValueError:
        day = None
    if not day and prompt:
        day = _extract_day(prompt)

    iso_date = _extract_date(data.get("date")) or (_extract_date(prompt) if prompt else None)
    if iso_date and not day:
        try:
            day = DAYS[date.fromisoformat(iso_date).weekday()]
        except ValueError:
            iso_date = None

    time_slot = _canonical_time_slot(data.get("time_slot"))
    if not time_slot and prompt:
        time_slot = _extract_time_slot(prompt)

    group_value = str(data.get("group") or "").strip().upper() or None
    if group_value in {"G1", "G2"}:
        badge = badge or ("Q1" if group_value == "G1" else "Q2")
    else:
        group_value = None
    if not group_value and badge:
        group_value = "G1" if badge == "Q1" else "G2"

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    return {
        "room": room,
        "day": day,
        "date": iso_date,
        "time_slot": time_slot,
        "week_badge": badge,
        "group": group_value,
        "class_name": str(data.get("class_name") or "").strip()[:100] or None,
        "teacher": str(data.get("teacher") or "").strip()[:100] or None,
        "subject": str(data.get("subject") or "").strip()[:100] or None,
        "confidence": confidence,
    }


def _apply_schedule_gate(
    decision: ScheduleQueryDecision,
    *,
    prompt: str,
    known_rooms: list[str] | None,
) -> tuple[ScheduleQueryDecision | None, str | None, bool]:
    """Hard-gate low confidence and invalid room actions.

    Returns ``(decision, note, fallback_used)``. A ``None`` decision means the
    caller should use the deterministic fallback response.
    """
    filters = _normalize_filters(decision, prompt=prompt, known_rooms=known_rooms)
    action = str(decision.action)

    if filters["confidence"] < gemini_service.SCHEDULE_CONFIDENCE_THRESHOLD:
        return (
            None,
            "Low routing confidence; used deterministic schedule parsing.",
            True,
        )

    note: str | None = None
    if action == "check_room_availability" and not filters["room"]:
        if filters["day"] or filters["date"] or filters["time_slot"] or filters["week_badge"]:
            action = "find_available_rooms"
            note = "Room check had no room; searched available rooms instead."
        else:
            action = "query_schedule"
            note = "Room check had no room; answered as a general schedule query."

    gated = ScheduleQueryDecision.model_validate({**filters, "action": action})
    return gated, note, False


def _build_schedule_prompt(prompt: str, known_rooms: list[str] | None) -> str:
    payload = {
        "today": date.today().isoformat(),
        "week_start": DEFAULT_WEEK_START,
        "known_rooms": sorted({str(room) for room in (known_rooms or [])}),
        "prompt": prompt,
    }
    return (
        "Classify this natural-language schedule question.\n\n"
        f"{payload}\n\n"
        "Return one action:\n"
        "- check_room_availability: the user asks whether a specific room is "
        "free/occupied. A room is required.\n"
        "- find_available_rooms: the user asks which rooms are free.\n"
        "- query_schedule: general questions about classes, teachers, subjects, "
        "rooms, days, times, or Q1/Q2 and G1/G2 sessions.\n\n"
        "Extract only filters explicitly present or unambiguously implied. "
        "Q1/Q2 are alternating-week badges and G1/G2 are derived groups; "
        "never treat them as physical rooms. Use canonical weekday names, ISO "
        "dates and HH:MM-HH:MM time ranges. If unsure, set confidence <= 0.5."
    )


async def route_schedule_query(
    prompt: str,
    *,
    known_rooms: list[str] | None = None,
) -> dict[str, Any]:
    """Route a schedule prompt with Gemini rotation and deterministic fallback."""
    known_rooms = list(known_rooms or [])
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    def fallback_response(
        error: str, note: str | None = None, decision: ScheduleQueryDecision | None = None
    ) -> dict[str, Any]:
        resolved = decision or _fallback_schedule_decision(prompt, known_rooms, error)
        return {
            "decision": resolved.model_dump(),
            "source": "fallback",
            "model": "fallback",
            "error": error,
            "note": note,
        }

    if not api_key:
        return fallback_response("GEMINI_API_KEY is not configured")
    if gemini_service.genai is None or gemini_service.types is None:
        return fallback_response(
            f"google-genai is unavailable: {gemini_service.GENAI_IMPORT_ERROR}"
        )

    models = gemini_service._model_rotation()
    per_attempt_timeout = gemini_service._configured_timeout()
    budget = gemini_service._total_budget()
    deadline = time.monotonic() + budget
    last_error = "All Gemini models failed"

    logger.info(
        "Gemini schedule router starting: models=%s timeout=%.1fs budget=%.1fs",
        ",".join(models),
        per_attempt_timeout,
        budget,
    )

    try:
        client = gemini_service.genai.Client(api_key=api_key)
    except Exception as exc:
        message = f"Gemini client init failed: {type(exc).__name__}: {exc}"[:300]
        return fallback_response(message)

    contents = _build_schedule_prompt(prompt, known_rooms)

    for model in models:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = f"Overall Gemini timeout budget ({budget:.1f}s) exhausted"
            break
        attempt_timeout = min(per_attempt_timeout, remaining)

        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=gemini_service.types.GenerateContentConfig(
                        system_instruction=(
                            "You are a precise schedule query router for an IT "
                            "department secretary. Answer only with the requested "
                            "JSON schema. Treat Q1/Q2 and G1/G2 as week badges or "
                            "groups, never physical rooms. Be conservative: low "
                            "confidence must be expressed as a number <= 0.5."
                        ),
                        temperature=0.0,
                        max_output_tokens=1024,
                        response_mime_type="application/json",
                        response_schema=ScheduleQueryDecision,
                    ),
                ),
                timeout=attempt_timeout,
            )
        except asyncio.TimeoutError:
            last_error = f"{model}: timeout after {attempt_timeout:.1f}s"
            logger.warning("Gemini schedule router model %s timed out; rotating", model)
            continue
        except Exception as exc:
            kind = gemini_service._gemini_error_kind(exc)
            last_error = f"{model}: {type(exc).__name__}: {exc}"[:300]
            if kind in {"transient", "not_found"}:
                logger.warning("Gemini schedule router rotating from %s: %s", model, last_error)
                continue
            logger.warning("Gemini schedule router fatal error on %s: %s", model, last_error)
            break

        try:
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, ScheduleQueryDecision):
                decision = parsed
            elif isinstance(parsed, dict):
                decision = ScheduleQueryDecision.model_validate(parsed)
            else:
                decision = ScheduleQueryDecision.model_validate(
                    gemini_service._extract_json(getattr(response, "text", "") or "")
                )
        except Exception as exc:
            last_error = (
                f"{model}: invalid routing response: {type(exc).__name__}: {exc}"
            )[:300]
            logger.warning("Gemini schedule router invalid response: %s", last_error)
            break

        gated, note, fallback_used = _apply_schedule_gate(
            decision, prompt=prompt, known_rooms=known_rooms
        )
        if fallback_used or gated is None:
            return fallback_response("low routing confidence", note=note)

        logger.info("Gemini schedule router succeeded with model %s", model)
        return {
            "decision": gated.model_dump(),
            "source": "gemini",
            "model": model,
            "error": None,
            "note": note,
        }

    return fallback_response(last_error)


def _short_session(entry: dict[str, Any], lang: str) -> str:
    when = entry.get("time_slot") or (
        f"{entry.get('start_time', '')}-{entry.get('end_time', '')}"
    )
    subject = entry.get("subject") or "—"
    room = entry.get("room") or "—"
    teacher = entry.get("teacher") or ""
    teacher_text = f" ({teacher})" if teacher else ""
    return f"{entry.get('day', '')} {when}: {subject}{teacher_text} — {room}"


def _format_context(filters: dict[str, Any], lang: str) -> str:
    parts: list[str] = []
    if filters.get("day"):
        parts.append(str(filters["day"]))
    if filters.get("time_slot"):
        parts.append(str(filters["time_slot"]))
    if filters.get("week_badge"):
        parts.append(str(filters["week_badge"]))
    if not parts:
        return ""
    connector = " le " if lang == "fr" else " on "
    return connector + ", ".join(parts)


def _build_answer(
    action: str,
    filters: dict[str, Any],
    lang: str,
    *,
    available: bool | None,
    conflicts: list[dict[str, Any]],
    available_rooms: list[str],
    matching_sessions: list[dict[str, Any]],
) -> str:
    context = _format_context(filters, lang)
    room = filters.get("room")

    if action == "check_room_availability" and room:
        if available:
            if lang == "fr":
                return f"La salle {room} est libre{context}."
            return f"Room {room} is free{context}."
        details = "; ".join(_short_session(item, lang) for item in conflicts[:3])
        if lang == "fr":
            suffix = f" : {details}" if details else ""
            return f"La salle {room} est occupée{context}{suffix}."
        suffix = f": {details}" if details else ""
        return f"Room {room} is occupied{context}{suffix}."

    if action == "find_available_rooms":
        count = len(available_rooms)
        preview = ", ".join(available_rooms[:12])
        if lang == "fr":
            if not count:
                return f"Aucune salle libre trouvée{context}."
            suffix = f" ({preview})" if preview else ""
            return f"{count} salle(s) libre(s){context}{suffix}."
        if not count:
            return f"No free rooms found{context}."
        suffix = f" ({preview})" if preview else ""
        return f"{count} room(s) are free{context}{suffix}."

    count = len(matching_sessions)
    preview = "; ".join(_short_session(item, lang) for item in matching_sessions[:3])
    if lang == "fr":
        suffix = f" : {preview}" if preview else ""
        return f"J'ai trouvé {count} entrée(s) d'emploi du temps{context}{suffix}."
    suffix = f": {preview}" if preview else ""
    return f"I found {count} matching schedule entries{context}{suffix}."


def _resolve_week(
    filters: dict[str, Any],
    *,
    week_start: str,
) -> tuple[str | None, str]:
    if filters.get("week_badge"):
        return str(filters["week_badge"]), week_start
    if filters.get("date"):
        try:
            return calculate_active_week(str(filters["date"]), week_start), week_start
        except ValueError:
            pass
    return None, week_start


async def execute_schedule_query(
    decision: ScheduleQueryDecision | dict[str, Any],
    prompt: str,
    *,
    known_rooms: list[str] | None = None,
    week_start: str = DEFAULT_WEEK_START,
) -> dict[str, Any]:
    """Execute a gated decision with read-only schedule queries."""
    filters = _normalize_filters(decision, prompt=prompt, known_rooms=known_rooms)
    action = str(
        decision.action
        if isinstance(decision, ScheduleQueryDecision)
        else decision.get("action", "query_schedule")
    )
    lang = _detect_language(prompt)

    if action in {"check_room_availability", "find_available_rooms"}:
        if not filters["day"]:
            filters["day"] = DAYS[date.today().weekday()]
    slots = parse_time_slot(filters["time_slot"])["slots"] if filters["time_slot"] else []
    active_week, resolved_week_start = _resolve_week(filters, week_start=week_start)
    filters["slots"] = slots
    filters["active_week"] = active_week
    filters["week_start"] = resolved_week_start
    filters["effective_day"] = filters["day"]

    try:
        if action == "check_room_availability" and filters["room"]:
            sessions = await fetch_occupancies(
                filters["day"], slots, active_week
            )
            conflicts = [
                item for item in sessions
                if str(item.get("room") or "").strip().upper() == filters["room"]
            ]
            available = not conflicts
            available_rooms: list[str] = []
            matching_sessions = conflicts
        elif action == "find_available_rooms":
            sessions = await fetch_occupancies(
                filters["day"], slots, active_week
            )
            catalogue = set(default_room_catalogue()) | {
                str(room).strip() for room in (known_rooms or [])
            }
            availability = compute_availability(catalogue, sessions)
            available_rooms = availability["available_room_names"]
            conflicts = sessions
            matching_sessions = sessions
            available = bool(available_rooms)
        else:
            matching_sessions = await query_schedule_entries(
                day=filters["day"],
                slots=slots or None,
                week=active_week,
                room=filters["room"],
                class_name=filters["class_name"],
                teacher=filters["teacher"],
                subject=filters["subject"],
            )
            conflicts = []
            available_rooms = []
            available = None
    except ScheduleStoreError as exc:
        raise RuntimeError(f"MongoDB schedule query failed: {exc}") from exc

    answer = _build_answer(
        action,
        filters,
        lang,
        available=available,
        conflicts=conflicts,
        available_rooms=available_rooms,
        matching_sessions=matching_sessions,
    )

    return {
        "action": action,
        "prompt": prompt,
        "filters": filters,
        "answer": answer,
        "available": available,
        "conflicts": conflicts,
        "available_rooms": available_rooms,
        "matching_sessions": matching_sessions,
    }


async def load_known_rooms() -> list[str]:
    """Return MongoDB rooms with the bundled catalogue as a safe fallback."""
    try:
        stored = await distinct_rooms()
    except ScheduleStoreError as exc:
        logger.warning("Could not load stored rooms for scheduling: %s", exc)
        stored = []
    return sorted(set(default_room_catalogue()) | set(stored))

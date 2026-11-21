"""Google Gemini integration for classroom recommendations.

The API key is optional: when it is missing or Gemini is unreachable, the
caller receives a deterministic local summary instead of a hard failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from pydantic import BaseModel, Field

try:  # Keep the API importable even if google-genai is not installed.
    from google import genai
    from google.genai import types

    GENAI_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - depends on the environment
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    GENAI_IMPORT_ERROR = str(exc)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"


class AIAdvice(BaseModel):
    """Structured Gemini response."""

    summary: str = Field(description="2-3 sentence summary of room availability")
    recommendation: str = Field(description="1-2 sentence recommendation for the secretary")
    recommended_room: str | None = Field(
        default=None, description="One room from the available_rooms list, or null"
    )


def _fallback_advice(
    day: str,
    time_slot: str,
    capacity_needed: int | None,
    available_rooms: list[dict[str, Any]],
    occupied_rooms: list[str],
    total_rooms: int,
    active_week: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    suitable = [room for room in available_rooms if room.get("suitable_for_capacity", True)]
    if capacity_needed is not None:
        known_suitable = [room for room in suitable if room.get("capacity_known")]
        preferred = known_suitable or suitable
    else:
        preferred = suitable
    names = [room["room"] for room in preferred]
    slot_phrase = f"on {day} during {time_slot}"
    if active_week:
        slot_phrase += f" in the active {active_week} week"

    if names:
        summary = f"{len(available_rooms)} of {total_rooms} rooms are free {slot_phrase}."
        recommendation = f"Suggested room: {names[0]}."
        if capacity_needed is not None:
            chosen = preferred[0]
            if chosen.get("capacity_known"):
                recommendation += (
                    f" It is free and has a known capacity of "
                    f"{chosen['capacity']} seats."
                )
            else:
                recommendation += (
                    f" Capacity data is not configured for {names[0]}, so verify "
                    f"that it seats at least {capacity_needed} people."
                )
        if occupied_rooms:
            recommendation += f" Occupied rooms: {', '.join(occupied_rooms)}."
    elif available_rooms:
        summary = (
            f"{len(available_rooms)} rooms are free {slot_phrase}, but none has "
            f"a known capacity of at least {capacity_needed}."
        )
        recommendation = (
            "No free room can be confirmed for the requested capacity; "
            "consider another time slot or verify room capacities."
        )
    else:
        summary = f"No classrooms are free {slot_phrase}."
        recommendation = (
            "Consider a different time slot, or check whether a lab/room "
            "assignment outside the timetable is available."
        )

    return {
        "summary": summary,
        "recommendation": recommendation,
        "recommended_room": names[0] if names else None,
        "source": "fallback",
        "model": None,
        "error": error,
    }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _build_prompt(
    day: str,
    time_slot: str,
    capacity_needed: int | None,
    available_rooms: list[dict[str, Any]],
    occupied_rooms: list[str],
    occupancies: list[dict[str, Any]],
    total_rooms: int,
    target_date: str | None = None,
    week_start: str | None = None,
    active_week: str | None = None,
) -> str:
    payload = {
        "day": day,
        "time_slot": time_slot,
        "target_date": target_date,
        "week_start": week_start,
        "active_week": active_week,
        "capacity_needed": capacity_needed,
        "total_rooms": total_rooms,
        "available_rooms": available_rooms,
        "suitable_room_names": [
            room["room"]
            for room in available_rooms
            if room.get("suitable_for_capacity", True)
        ],
        "occupied_room_codes": occupied_rooms,
        "occupancies": occupancies,
        "capacity_data_configured": any(room.get("capacity_known") for room in available_rooms),
    }
    return (
        "You are the scheduling assistant for the IT department secretary at "
        "ISET Radès. A teacher or secretary wants a classroom for the slot below.\n\n"
        f"Availability data:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "Write a concise, human-friendly summary and a smart recommendation. "
        "Rules:\n"
        "- ONLY recommend a room from available_rooms; never invent a room.\n"
        "- Prefer suitable_room_names; never recommend a room marked "
        "suitable_for_capacity=false.\n"
        "- Occupancy has already been filtered to the active alternating week "
        "(Q1/Q2); mention the active week when it adds useful context.\n"
        "- If capacity_needed is set and capacity data is missing, say that the "
        "capacity must be verified instead of guessing.\n"
        "- Mention notable occupied rooms/classes when useful.\n"
        "- If no room is available, say so and suggest checking another slot.\n"
        "- Respond in English, 2-3 sentences for summary and 1-2 for recommendation.\n"
        'Return JSON with keys: "summary", "recommendation", "recommended_room".'
    )


async def generate_ai_advice(
    day: str,
    time_slot: str,
    capacity_needed: int | None,
    available_rooms: list[dict[str, Any]],
    occupied_rooms: list[str],
    occupancies: list[dict[str, Any]],
    total_rooms: int,
    target_date: str | None = None,
    week_start: str | None = None,
    active_week: str | None = None,
) -> dict[str, Any]:
    """Ask Gemini for a summary/recommendation, with a local fallback."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    if not api_key:
        return _fallback_advice(
            day,
            time_slot,
            capacity_needed,
            available_rooms,
            occupied_rooms,
            total_rooms,
            active_week=active_week,
            error="GEMINI_API_KEY is not configured",
        )
    if genai is None or types is None:
        return _fallback_advice(
            day,
            time_slot,
            capacity_needed,
            available_rooms,
            occupied_rooms,
            total_rooms,
            active_week=active_week,
            error=f"google-genai is unavailable: {GENAI_IMPORT_ERROR}",
        )

    try:
        timeout = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "20"))
        timeout = min(max(timeout, 5.0), 60.0)

        client = genai.Client(api_key=api_key)
        prompt = _build_prompt(
            day,
            time_slot,
            capacity_needed,
            available_rooms,
            occupied_rooms,
            occupancies,
            total_rooms,
            target_date=target_date,
            week_start=week_start,
            active_week=active_week,
        )
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You are a precise scheduling assistant. Never invent "
                        "rooms or availability that are not in the supplied data."
                    ),
                    temperature=0.2,
                    max_output_tokens=512,
                    response_mime_type="application/json",
                    response_schema=AIAdvice,
                ),
            ),
            timeout=timeout,
        )

        parsed_response = getattr(response, "parsed", None)
        if isinstance(parsed_response, AIAdvice):
            advice = parsed_response
        elif isinstance(parsed_response, dict):
            advice = AIAdvice.model_validate(parsed_response)
        else:
            text = (getattr(response, "text", "") or "").strip()
            advice = AIAdvice.model_validate(_extract_json(text))

        available_names = {room["room"] for room in available_rooms}
        suitable_names = {
            room["room"]
            for room in available_rooms
            if room.get("suitable_for_capacity", True)
        }
        recommended = (advice.recommended_room or "").strip().upper() or None
        if recommended and recommended not in available_names:
            raise ValueError(
                f"Gemini recommended unavailable room '{advice.recommended_room}'"
            )
        if recommended and recommended not in suitable_names:
            raise ValueError(
                f"Gemini recommended a room that does not meet the capacity: "
                f"'{advice.recommended_room}'"
            )

        return {
            "summary": advice.summary.strip(),
            "recommendation": advice.recommendation.strip(),
            "recommended_room": recommended,
            "source": "gemini",
            "model": model,
            "error": None,
        }
    except asyncio.TimeoutError:
        logger.warning("Gemini recommendation timed out after %.1fs", timeout)
        error = "Gemini request timed out"
    except Exception as exc:
        logger.warning("Gemini recommendation failed: %s", exc)
        error = f"{type(exc).__name__}: {exc}"[:300]

    return _fallback_advice(
        day,
        time_slot,
        capacity_needed,
        available_rooms,
        occupied_rooms,
        total_rooms,
        active_week=active_week,
        error=error,
    )

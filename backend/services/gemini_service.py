"""Google Gemini integration for classroom recommendations and Word routing.

The API key is optional: when it is missing or Gemini is unreachable, Word
requests receive a deterministic routing decision with a complete ``new_doc``
and ``design_spec`` instead of a hard failure. Blueprint routes are allowlisted
and hard-gated when no confident template match exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Literal

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

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TOTAL_BUDGET_SECONDS = 75.0
BLUEPRINT_CONFIDENCE_THRESHOLD = 0.7
SCHEDULE_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
)

TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
NOT_FOUND_STATUS_CODES = frozenset({404})
FATAL_STATUS_CODES = frozenset({400, 401, 403})


class AIAdvice(BaseModel):
    """Structured Gemini response."""

    summary: str = Field(description="2-3 sentence summary of room availability")
    recommendation: str = Field(description="1-2 sentence recommendation for the secretary")
    recommended_room: str | None = Field(
        default=None, description="One room from the available_rooms list, or null"
    )


class ScheduleQueryDecision(BaseModel):
    """Structured query plan for the unified schedule Q&A endpoint."""

    action: Literal[
        "check_room_availability",
        "find_available_rooms",
        "query_schedule",
    ] = Field(description="Which deterministic schedule query to run.")
    room: str | None = Field(
        default=None, description="Physical room code. Q1/Q2 are not rooms."
    )
    day: str | None = Field(default=None, description="Normalized weekday name.")
    date: str | None = Field(default=None, description="ISO date, when supplied.")
    time_slot: str | None = Field(default=None, description="Canonical time range.")
    week_badge: str | None = Field(default=None, description="Q1 or Q2 badge.")
    group: str | None = Field(default=None, description="Derived G1 or G2 group.")
    class_name: str | None = Field(default=None, description="Optional class filter.")
    teacher: str | None = Field(default=None, description="Optional teacher filter.")
    subject: str | None = Field(default=None, description="Optional subject filter.")
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Router confidence; low values fall back to deterministic parsing.",
    )


class WordEdit(BaseModel):
    """One targeted modification to apply to a Word blueprint."""

    action: Literal[
        "set_date",
        "set_title",
        "set_header",
        "set_table_header",
        "set_signature",
        "set_paragraph",
        "add_item",
        "update_item",
        "remove_item",
        "insert_section",
    ] = Field(description="Targeted edit to apply to the existing document.")
    table_index: int | None = Field(
        default=None,
        description=(
            "0-based table index. Omit it to target the most recent department "
            "table, which is the safe default for documents with historical sections."
        ),
    )
    row_index: int | None = Field(
        default=None,
        description="0-based absolute table row index (0 is the header row).",
    )
    match: str | None = Field(
        default=None,
        description="Existing designation/text to locate for update/remove/replace.",
    )
    value: str | None = Field(
        default=None, description="Replacement text, date or signature name."
    )
    designation: str | None = Field(
        default=None, description="Item designation for add/update."
    )
    quantity: str | None = Field(
        default=None, description="Item quantity for add/update."
    )
    heading: str | None = Field(
        default=None, description="Heading or signature title for insert_section/set_signature."
    )
    body: str | None = Field(
        default=None, description="Paragraph text or department line."
    )
    columns: list[str] = Field(
        default_factory=list, description="Column labels for set_table_header."
    )


class WordSection(BaseModel):
    """A structured section for a standalone Word document."""

    heading: str | None = Field(default=None, description="Section heading.")
    body: str | None = Field(default=None, description="Paragraph text, newline separated.")
    columns: list[str] = Field(
        default_factory=list, description="Table column labels."
    )
    rows: list[list[str]] = Field(
        default_factory=list, description="Table rows, each with one value per column."
    )


class WordNewSection(BaseModel):
    """A content block in a document created from scratch."""

    heading: str | None = Field(default=None, description="Optional section heading.")
    paragraphs: list[str] = Field(
        default_factory=list, description="Body paragraphs for this section."
    )
    bullets: list[str] = Field(default_factory=list, description="Bullet list items.")
    numbered_items: list[str] = Field(
        default_factory=list, description="Numbered list items."
    )


class WordNewTable(BaseModel):
    """A structured table in a document created from scratch."""

    title: str | None = Field(default=None, description="Optional table caption.")
    headers: list[str] = Field(default_factory=list, description="Column headers.")
    rows: list[list[str]] = Field(default_factory=list, description="Table rows.")


class WordSignatureBlock(BaseModel):
    """Optional sign-off for official or administrative documents."""

    title: str | None = Field(default=None, description="Signature title/role line.")
    role: str | None = Field(default=None, description="Alternative role line.")
    name: str | None = Field(default=None, description="Signatory name.")
    organization: str | None = Field(
        default=None, description="Optional organization/department line."
    )


class WordNewDocument(BaseModel):
    """Complete structure for a clean document generated without a blueprint."""

    title: str = Field(default="", description="Document title.")
    subtitle: str | None = Field(default=None, description="Optional subtitle.")
    date_line: str | None = Field(
        default=None,
        description="Optional date line; only when relevant to the request/context.",
    )
    header_note: str | None = Field(
        default=None, description="Optional note under the title area."
    )
    sections: list[WordNewSection] = Field(
        default_factory=list, description="Ordered content blocks."
    )
    tables: list[WordNewTable] = Field(
        default_factory=list, description="Structured tables."
    )
    signature_block: WordSignatureBlock | None = Field(
        default=None,
        description="Only for official/administrative documents when appropriate.",
    )


class WordDesignBlock(BaseModel):
    """One ordered block in a Gemini-controlled ``create_new`` layout."""

    kind: Literal[
        "title_block",
        "heading",
        "paragraph",
        "callout",
        "bullet_list",
        "numbered_list",
        "table",
        "page_break",
        "signature_block",
    ]
    title: str | None = Field(default=None, description="Title or callout heading.")
    subtitle: str | None = Field(default=None, description="Subtitle under a title.")
    kicker: str | None = Field(
        default=None, description="Small accent line above a title."
    )
    level: int | None = Field(
        default=None, description="Heading level (1-6); sanitized by the renderer."
    )
    text: str | None = Field(
        default=None, description="Paragraph or callout body text."
    )
    bold: bool | None = Field(default=None, description="Bold paragraph text.")
    italic: bool | None = Field(default=None, description="Italic paragraph text.")
    align: str | None = Field(
        default=None,
        description="left, center, right or justify; sanitized by the renderer.",
    )
    items: list[str] = Field(
        default_factory=list, description="Items for bullet/numbered lists."
    )
    caption: str | None = Field(default=None, description="Optional table caption.")
    headers: list[str] = Field(default_factory=list, description="Table column headers.")
    rows: list[list[str]] = Field(default_factory=list, description="Table rows.")
    fill_hex: str | None = Field(
        default=None, description="Callout background override."
    )
    border_hex: str | None = Field(
        default=None, description="Callout/table border override."
    )
    signatory_role: str | None = Field(
        default=None, description="Role/title line in a signature block."
    )
    signatory_name: str | None = Field(
        default=None, description="Name line in a signature block."
    )


class WordDesignSpec(BaseModel):
    """Page-level styling and ordered layout blocks for a new document."""

    font_name: str = Field(default="Calibri", description="Base font family.")
    base_font_size_pt: float = Field(
        default=11.0, description="Base font size; renderer clamps to 8-36 pt."
    )
    margin_inches: float = Field(
        default=0.85, description="Uniform margin; renderer clamps to 0.4-2.0 in."
    )
    orientation: Literal["portrait", "landscape"] = Field(
        default="portrait", description="Page orientation."
    )
    primary_hex: str = Field(default="#1B365D", description="Headings/primary color.")
    accent_hex: str = Field(default="#2B6CB0", description="Accents and kickers.")
    callout_bg_hex: str = Field(
        default="#F0F4F8", description="Default callout background."
    )
    table_header_hex: str = Field(
        default="#1B365D", description="Table header background."
    )
    table_stripe_hex: str = Field(
        default="#F7FAFC", description="Alternating table row background."
    )
    header_text: str | None = Field(default=None, description="Running page header.")
    footer_text: str | None = Field(default=None, description="Running page footer.")
    include_page_numbers: bool = Field(
        default=False, description="Append a PAGE field to the footer."
    )
    blocks: list[WordDesignBlock] = Field(
        default_factory=list, description="Ordered document blocks."
    )


class WordPlan(BaseModel):
    """Structured Word generation/editing plan returned by Gemini."""

    title: str | None = Field(default=None, description="Document title.")
    summary: str = Field(default="", description="Short summary of the proposed changes.")
    signature_title: str | None = Field(
        default=None, description="Signature title for standalone documents."
    )
    signature_name: str | None = Field(
        default=None, description="Signatory name for standalone documents."
    )
    edits: list[WordEdit] = Field(
        default_factory=list, description="Targeted edits for an existing blueprint."
    )
    sections: list[WordSection] = Field(
        default_factory=list, description="Sections for a standalone document."
    )


class WordRouteDecision(BaseModel):
    """Routing decision for a free-form Word document request."""

    action: Literal["use_blueprint", "modify_blueprint", "create_new"] = Field(
        description=(
            "use_blueprint: use an allowlisted template with no/minimal changes; "
            "modify_blueprint: apply targeted edits to an allowlisted template; "
            "create_new: build a clean document from design_spec/new_doc."
        )
    )
    blueprint_id: str | None = Field(
        default=None,
        description="An id from the allowlisted blueprint catalog, or null.",
    )
    title: str | None = Field(default=None, description="Document title.")
    summary: str = Field(default="", description="Concise user-facing summary.")
    explanation: str = Field(
        default="", description="Brief explanation of the routing decision."
    )
    signature_title: str | None = Field(
        default=None, description="Signature title for a new document."
    )
    signature_name: str | None = Field(
        default=None, description="Signatory name for a new document."
    )
    edits: list[WordEdit] = Field(
        default_factory=list, description="Targeted edits for blueprint modes."
    )
    sections: list[WordSection] = Field(
        default_factory=list, description="Legacy sections/tables for create_new."
    )
    blueprint_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "How confident Gemini is that the blueprint truly fits. In Auto "
            "mode low/ambiguous confidence is hard-gated to create_new."
        ),
    )
    new_doc: WordNewDocument | None = Field(
        default=None,
        description="Complete structure for create_new documents.",
    )
    design_spec: WordDesignSpec | None = Field(
        default=None,
        description="Gemini-controlled styling and block order for create_new.",
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


# ---------------------------------------------------------------------------
# Word document planner
# ---------------------------------------------------------------------------


def _clean_instruction_line(line: str) -> str:
    return line.strip().strip(".-•* ")


def _parse_instruction_edits(instructions: str | None) -> list[dict[str, Any]]:
    """Parse simple English/French instructions into deterministic edits.

    This intentionally covers only explicit, unambiguous requests. Anything
    more complex is left to Gemini; the fallback must never invent content.
    """
    edits: list[dict[str, Any]] = []
    source_text = instructions or ""

    # Dates are often appended after another instruction ("..., date 22/11/2026"),
    # so extract them from the whole request before splitting into clauses.
    for date_match in re.finditer(
        r"\b(?:date|dated|dat[ée]e?)\b[^0-9]{0,20}"
        r"(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}-\d{2}-\d{2})",
        source_text,
        re.IGNORECASE,
    ):
        edit = {"action": "set_date", "value": date_match.group(1)}
        if edit not in edits:
            edits.append(edit)

    if not any(edit["action"] == "set_date" for edit in edits) and re.search(
        r"\b(?:today|aujourd'hui|current date|date du jour)\b",
        source_text,
        re.IGNORECASE,
    ):
        edits.append(
            {"action": "set_date", "value": date.today().strftime("%d/%m/%Y")}
        )

    parts = re.split(r"[\n;]+", source_text)
    expanded: list[str] = []
    for part in parts:
        expanded.extend(
            re.split(
                r"\s+(?:and|then|puis|et)\s+"
                r"(?=(?:add|remove|delete|update|change|modify|set|"
                r"supprimer|ajouter|modifier|mettre)\b)",
                part,
                flags=re.IGNORECASE,
            )
        )

    for raw_line in expanded:
        line = _clean_instruction_line(raw_line)
        if not line:
            continue

        # Dates were already extracted from the full request above. Strip the
        # date clause here so a trailing "..., date 22/11/2026" does not hide
        # another instruction on the same line (for example "remove scotch").
        line = re.sub(
            r"(?:[,;]?\s*\b(?:date|dated|dat[ée]e?)\b[^0-9]{0,20}"
            r"(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}-\d{2}-\d{2}"
            r"|today|aujourd'hui|current date|date du jour)).*$",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip(" ,;")
        if not line:
            continue

        title_match = re.match(
            r"(?:title|titre|objet|header|en-t[êe]te)\s*(?:to|:|=)\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if title_match:
            edits.append({"action": "set_title", "value": title_match.group(1).strip()})
            continue

        signature_match = re.match(
            r"(?:signature|sign[ée]e?\s+par)\s*(?:to|:|=)\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if signature_match:
            edits.append(
                {"action": "set_signature", "value": signature_match.group(1).strip()}
            )
            continue

        remove_match = re.match(
            r"(?:remove|delete|supprimer|retirer|enlever)\s+(?:item\s+)?"
            r"(.+?)(?:\s+(?:from|on|in|du|de la|dans)\b.*)?$",
            line,
            re.IGNORECASE,
        )
        if remove_match:
            edits.append(
                {"action": "remove_item", "match": remove_match.group(1).strip()}
            )
            continue

        update_match = re.match(
            r"(?:update|change|modify|mettre\s+à\s+jour|modifier)\s+"
            r"(.+?)\s+(?:to|->|=>|à|a)\s+(.+)$",
            line,
            re.IGNORECASE,
        )
        if update_match:
            old_designation = update_match.group(1).strip()
            remainder = update_match.group(2).strip()
            quantity = None
            quantity_match = re.search(
                r"(?:,|;)?\s*(?:qty|quantity|quantit[ée])\s*[:=]?\s*(\d+)",
                remainder,
                re.IGNORECASE,
            )
            if quantity_match:
                quantity = quantity_match.group(1)
                remainder = remainder[: quantity_match.start()].strip(" ,;")
            edit: dict[str, Any] = {
                "action": "update_item",
                "match": old_designation,
                "designation": remainder,
            }
            if quantity is not None:
                edit["quantity"] = quantity
            edits.append(edit)
            continue

        add_match = re.match(
            r"(?:add|ajouter|ins[ée]rer|ajoute)\s+(.+?)"
            r"(?:\s*(?:,|;|\bqty\b|\bquantity\b|\bquantit[ée]\b|\bx\b|×)\s*(\d+))\s*$",
            line,
            re.IGNORECASE,
        )
        if add_match:
            edits.append(
                {
                    "action": "add_item",
                    "designation": add_match.group(1).strip(),
                    "quantity": add_match.group(2),
                }
            )
            continue

        add_quantity_first = re.match(
            r"(?:add|ajouter|ins[ée]rer|ajoute)\s+(\d+)\s+(.+)",
            line,
            re.IGNORECASE,
        )
        if add_quantity_first:
            edits.append(
                {
                    "action": "add_item",
                    "designation": add_quantity_first.group(2).strip(),
                    "quantity": add_quantity_first.group(1),
                }
            )
            continue

        add_plain = re.match(
            r"(?:add|ajouter|ins[ée]rer|ajoute)\s+(?:item\s+)?(.+)",
            line,
            re.IGNORECASE,
        )
        if add_plain:
            edits.append(
                {
                    "action": "add_item",
                    "designation": add_plain.group(1).strip(),
                    "quantity": "01",
                }
            )

    deduped: list[dict[str, Any]] = []
    for edit in edits:
        if edit not in deduped:
            deduped.append(edit)
    return deduped


def _plain(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


def _catalog_entry(
    catalog: list[dict[str, Any]] | None, identifier: str | None
) -> dict[str, Any] | None:
    if not identifier:
        return None
    key = str(identifier).strip().casefold()
    for entry in catalog or []:
        if str(entry.get("id") or "").casefold() == key:
            return entry
    return None


def _match_catalog_entry(
    prompt: str, catalog: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """Find the most specific allowlisted blueprint mentioned by the prompt."""
    text = _plain(prompt)
    best: tuple[int, dict[str, Any]] | None = None

    for entry in catalog or []:
        identifiers = [
            str(entry.get("id") or ""),
            str(entry.get("filename") or ""),
            str(Path(str(entry.get("filename") or "")).stem),
            str(entry.get("title") or ""),
        ]
        score = 0
        for identifier in identifiers:
            key = _plain(identifier)
            if len(key) < 4:
                continue
            if key in text:
                score = max(score, len(key) + 20)
            elif len(text) >= 6 and text in key:
                score = max(score, len(text) + 10)

        if "besoin" in text and "besoin" in _plain(
            f"{entry.get('filename')} {entry.get('title')}"
        ):
            score = max(score, 10)

        if score and (best is None or score > best[0]):
            best = (score, entry)

    return best[1] if best else None


def _has_modification_intent(text: str) -> bool:
    normalized = _plain(text)
    markers = (
        "add ",
        "add:",
        "remove",
        "delete",
        "update",
        "change",
        "modify",
        "edit",
        "set ",
        "date",
        "signature",
        "title",
        "header",
        "ajouter",
        "supprimer",
        "modifier",
        "mettre a jour",
        "changer",
        "remplacer",
        "titre",
        "en-tete",
        "date",
    )
    return any(marker in normalized for marker in markers)


def _has_create_intent(text: str) -> bool:
    normalized = _plain(text)
    markers = (
        "create",
        "new ",
        "generate",
        "draft",
        "build",
        "redige",
        "nouveau",
        "nouvelle",
        "creer",
        "generer",
        "composer",
    )
    return any(marker in normalized for marker in markers)


def _has_use_intent(text: str) -> bool:
    normalized = _plain(text)
    markers = (
        "use ",
        "using",
        "based on",
        "template",
        "modele",
        "from the",
        "a partir",
        "utiliser",
    )
    return any(marker in normalized for marker in markers)


def _has_from_scratch_intent(text: str) -> bool:
    """Detect explicit requests that must never use a blueprint."""
    normalized = _plain(text)
    markers = (
        "from scratch",
        "from zero",
        "start fresh",
        "without blueprint",
        "no blueprint",
        "without template",
        "no template",
        "brand new document",
        "new document",
        "create new",
        "sans modele",
        "nouveau document",
        "document neuf",
        "de zero",
        "a partir de zero",
    )
    return any(marker in normalized for marker in markers)


def _has_institutional_context(text: str) -> bool:
    normalized = _plain(text)
    markers = (
        "iset",
        "rades",
        "departement",
        "note de service",
        "liste des besoins",
        "administratif",
        "administrative",
    )
    return any(marker in normalized for marker in markers)


def _fallback_new_doc(
    subject: str,
    instructions: str | None,
    target_class: str | None,
    schedules: list[dict[str, Any]] | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build a clean, structured new_doc without inventing content."""
    plan = _fallback_word_plan(
        subject,
        instructions,
        target_class,
        None,
        schedules,
        error=error,
        include_new_doc=False,
    )
    context = f"{subject} {instructions or ''}"
    institutional = _has_institutional_context(context)

    sections: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []

    for item in plan.get("sections") or []:
        heading = str(item.get("heading") or "").strip()
        body = str(item.get("body") or "").strip()
        columns = item.get("columns") or []
        rows = item.get("rows") or []

        if heading.casefold() == "informations" and not institutional:
            continue

        if columns and rows:
            tables.append(
                {
                    "title": heading or None,
                    "headers": [str(column) for column in columns],
                    "rows": [
                        ["" if value is None else str(value) for value in row]
                        for row in rows
                    ],
                }
            )
            continue

        section: dict[str, Any] = {
            "heading": heading or None,
            "paragraphs": [line.strip() for line in body.splitlines() if line.strip()],
            "bullets": [],
            "numbered_items": [],
        }
        if section["paragraphs"] or section["heading"]:
            sections.append(section)

    if not sections:
        sections.append(
            {
                "heading": "Contenu",
                "paragraphs": [
                    line.strip()
                    for line in str(subject or instructions or "Document").splitlines()
                    if line.strip()
                ],
                "bullets": [],
                "numbered_items": [],
            }
        )

    date_edits = [
        edit
        for edit in plan.get("edits") or []
        if edit.get("action") == "set_date"
    ]
    date_line: str | None = None
    if date_edits:
        date_line = str(date_edits[0].get("value") or "").strip() or None
    elif institutional:
        date_line = f"Radès, le {date.today():%d/%m/%Y}"

    signature_block: dict[str, Any] | None = None
    if "signature" in _plain(f"{subject} {instructions or ''}"):
        signature_block = {
            "title": plan.get("signature_title") or "Le Chef du Département",
            "role": None,
            "name": plan.get("signature_name") or None,
            "organization": (
                "Technologies de l'Informatique" if institutional else None
            ),
        }

    return {
        "title": str(plan.get("title") or subject or "Document"),
        "subtitle": None,
        "date_line": date_line,
        "header_note": None,
        "sections": sections,
        "tables": tables,
        "signature_block": signature_block,
    }


def _fallback_design_spec(
    new_doc: dict[str, Any] | None,
    prompt: str,
    target_class: str | None,
    schedules: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Convert a fallback ``new_doc`` into a clean, ordered design_spec."""
    document = new_doc or {}
    blocks: list[dict[str, Any]] = []

    title = str(document.get("title") or "").strip() or str(prompt or "Document").strip()
    subtitle = str(document.get("subtitle") or "").strip()
    if title or subtitle:
        blocks.append(
            {
                "kind": "title_block",
                "title": title or None,
                "subtitle": subtitle or None,
                "align": "center",
            }
        )

    date_line = str(document.get("date_line") or "").strip()
    if date_line:
        blocks.append(
            {
                "kind": "paragraph",
                "text": date_line,
                "align": "right",
                "italic": True,
            }
        )

    header_note = str(document.get("header_note") or "").strip()
    if header_note:
        blocks.append({"kind": "callout", "text": header_note})

    if target_class:
        blocks.append(
            {
                "kind": "paragraph",
                "text": f"Classe : {target_class}",
                "bold": True,
            }
        )

    for section in document.get("sections") or []:
        data = section if isinstance(section, dict) else {}
        heading = str(data.get("heading") or "").strip()
        if heading:
            blocks.append({"kind": "heading", "text": heading, "level": 1})
        for paragraph in data.get("paragraphs") or []:
            value = str(paragraph).strip()
            if value:
                blocks.append({"kind": "paragraph", "text": value})
        bullets = [str(item).strip() for item in data.get("bullets") or []]
        bullets = [item for item in bullets if item]
        if bullets:
            blocks.append({"kind": "bullet_list", "items": bullets})
        numbered = [str(item).strip() for item in data.get("numbered_items") or []]
        numbered = [item for item in numbered if item]
        if numbered:
            blocks.append({"kind": "numbered_list", "items": numbered})

    has_schedule_table = False
    for table in document.get("tables") or []:
        data = table if isinstance(table, dict) else {}
        headers = [str(value).strip() for value in data.get("headers") or []]
        rows = [
            ["" if value is None else str(value) for value in row]
            for row in data.get("rows") or []
        ]
        normalized = _plain(" ".join(headers))
        if "jour" in normalized and ("horaire" in normalized or "heure" in normalized):
            has_schedule_table = True
        caption = str(data.get("title") or "").strip()
        if caption:
            blocks.append({"kind": "heading", "text": caption, "level": 2})
        if headers:
            blocks.append(
                {
                    "kind": "table",
                    "caption": None,
                    "headers": headers,
                    "rows": rows,
                }
            )

    if schedules and not has_schedule_table:
        blocks.append(
            {
                "kind": "table",
                "caption": "Emploi du temps",
                "headers": ["Jour", "Horaire", "Matière", "Enseignant", "Salle", "Semaine"],
                "rows": [
                    [
                        str(entry.get("day") or ""),
                        str(entry.get("time_slot") or ""),
                        str(entry.get("subject") or ""),
                        str(entry.get("teacher") or ""),
                        str(entry.get("room") or ""),
                        str(entry.get("week") or ""),
                    ]
                    for entry in schedules
                ],
            }
        )

    signature = document.get("signature_block")
    if isinstance(signature, dict):
        role = str(
            signature.get("title") or signature.get("role") or ""
        ).strip()
        name = str(signature.get("name") or "").strip()
        if role or name:
            blocks.append(
                {
                    "kind": "signature_block",
                    "signatory_role": role or None,
                    "signatory_name": name or None,
                }
            )

    return {
        "font_name": "Calibri",
        "base_font_size_pt": 11.0,
        "margin_inches": 0.85,
        "orientation": "portrait",
        "primary_hex": "#1B365D",
        "accent_hex": "#2B6CB0",
        "callout_bg_hex": "#F0F4F8",
        "table_header_hex": "#1B365D",
        "table_stripe_hex": "#F7FAFC",
        "header_text": None,
        "footer_text": None,
        "include_page_numbers": False,
        "blocks": blocks,
    }


def _fallback_word_route(
    prompt: str,
    target_class: str | None,
    catalog: list[dict[str, Any]] | None,
    schedules: list[dict[str, Any]] | None,
    explicit_blueprint_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Deterministic intent routing used when Gemini is unavailable."""
    explicit_entry = _catalog_entry(catalog, explicit_blueprint_id)
    matched = explicit_entry or _match_catalog_entry(prompt, catalog)
    edits = _parse_instruction_edits(prompt)

    # An explicit from-scratch request in Auto mode always wins over a
    # coincidental mention of an allowlisted template.
    if explicit_entry is None and _has_from_scratch_intent(prompt):
        matched = None

    if matched is not None:
        blueprint_id = str(matched.get("id"))
        title = str(matched.get("title") or matched.get("filename") or blueprint_id)

        if explicit_entry is not None:
            confidence = 1.0
        else:
            confidence = 0.5
            normalized_prompt = _plain(prompt)
            identifiers = (
                str(matched.get("id") or ""),
                str(matched.get("filename") or ""),
                str(Path(str(matched.get("filename") or "")).stem),
                str(matched.get("title") or ""),
            )
            for identifier in identifiers:
                key = _plain(identifier)
                if len(key) >= 4 and key in normalized_prompt:
                    confidence = 0.95
                    break
            else:
                if "besoin" in normalized_prompt and "besoin" in _plain(
                    f"{matched.get('filename')} {matched.get('title')}"
                ):
                    confidence = 0.8

        if explicit_entry is not None:
            action = (
                "modify_blueprint"
                if edits or _has_modification_intent(prompt)
                else "use_blueprint"
            )
        elif edits:
            only_dates = all(edit.get("action") == "set_date" for edit in edits)
            action = "use_blueprint" if only_dates else "modify_blueprint"
        elif _has_use_intent(prompt):
            action = "use_blueprint"
        elif _has_create_intent(prompt):
            action = "create_new"
        else:
            action = "use_blueprint"

        if action in {"use_blueprint", "modify_blueprint"}:
            if action == "use_blueprint":
                edits = [
                    edit for edit in edits if edit.get("action") == "set_date"
                ]
                summary = f"Le modèle « {title} » a été utilisé."
                explanation = "Le prompt référence un modèle de l'allowlist."
            else:
                summary = f"Modifications ciblées appliquées au modèle « {title} »."
                explanation = "Le prompt demande des modifications sur un modèle autorisé."
            return {
                "action": action,
                "blueprint_id": blueprint_id,
                "title": title,
                "summary": summary,
                "explanation": explanation,
                "signature_title": None,
                "signature_name": None,
                "edits": edits,
                "sections": [],
                "blueprint_confidence": confidence,
                "new_doc": None,
                "design_spec": None,
                "_fallback_error": error,
            }

    plan = _fallback_word_plan(
        prompt,
        prompt,
        target_class,
        None,
        schedules,
        error=error,
        include_new_doc=False,
    )
    new_doc = _fallback_new_doc(prompt, prompt, target_class, schedules, error=error)
    explanation = (
        "L'utilisateur demande explicitement un document sans modèle."
        if _has_from_scratch_intent(prompt)
        else "Aucun modèle autorisé ne correspond clairement à la demande."
    )
    return {
        "action": "create_new",
        "blueprint_id": None,
        "title": new_doc.get("title") or plan.get("title") or prompt[:140],
        "summary": "Un nouveau document structuré a été préparé.",
        "explanation": explanation,
        "signature_title": plan.get("signature_title"),
        "signature_name": plan.get("signature_name"),
        "edits": [],
        "sections": plan.get("sections") or [],
        "blueprint_confidence": None,
        "new_doc": new_doc,
        "design_spec": _fallback_design_spec(
            new_doc, prompt, target_class, schedules
        ),
        "_fallback_error": error,
    }


def _build_route_prompt(
    prompt: str,
    target_class: str | None,
    catalog: list[dict[str, Any]] | None,
    schedules: list[dict[str, Any]] | None,
    explicit_blueprint_id: str | None,
) -> str:
    compact_catalog = [
        {
            "id": entry.get("id"),
            "filename": entry.get("filename"),
            "title": entry.get("title"),
            "description": entry.get("description"),
            "structure": entry.get("structure"),
        }
        for entry in catalog or []
    ]
    payload = {
        "today": date.today().isoformat(),
        "prompt": prompt,
        "target_class": target_class,
        "explicit_blueprint_id": explicit_blueprint_id,
        "allowlisted_blueprints": compact_catalog,
        "schedules": schedules or [],
    }
    return (
        "You route free-form Word document requests for the IT department "
        "secretary at ISET Radès.\n\n"
        f"Request:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "Choose exactly one action:\n"
        "- use_blueprint: the user wants to use/fill an allowlisted template "
        "with no changes or only an optional date update.\n"
        "- modify_blueprint: the user asks for targeted changes to an "
        "allowlisted template (add/remove/update items, date, header, "
        "signature, insert a section).\n"
        "- create_new: no allowlisted template fits, the user explicitly wants "
        "a brand-new document, or blueprint confidence is low/ambiguous. "
        "Populate both new_doc and a complete design_spec.\n\n"
        "Auto mode (explicit_blueprint_id is null):\n"
        "- Evaluate whether an allowlisted blueprint TRULY fits. Choose a "
        "blueprint action only with a clear match and blueprint_confidence "
        ">= 0.7; otherwise choose create_new with blueprint_id = null.\n"
        "- Phrases such as 'from scratch', 'without blueprint', 'new document', "
        "'sans modèle', 'nouveau document', or 'de zéro' always mean create_new.\n"
        "- A generic needs-list request may use the Liste des Besoins blueprint; "
        "unrelated topics must use create_new.\n\n"
        "Hard rules:\n"
        "- blueprint_id MUST be an id from allowlisted_blueprints, or null.\n"
        "- For use_blueprint/modify_blueprint, return targeted edits and set "
        "blueprint_confidence. For use_blueprint, only set_date edits are allowed.\n"
        "- For create_new, set blueprint_id = null. Populate new_doc AND a "
        "complete design_spec with ordered blocks. Block kinds: title_block, "
        "heading, paragraph, callout, bullet_list, numbered_list, table, "
        "page_break, signature_block. Set design_spec styling (font_name, "
        "base_font_size_pt, margin_inches, orientation, palette colors, optional "
        "header_text/footer_text/include_page_numbers) to suit the topic and "
        "language of the prompt.\n"
        "- Use design_spec.blocks to control visual order. Use callout for "
        "highlighted notes, table for structured data, and page_break only when "
        "the document genuinely needs a new page.\n"
        "- Only set date_line, header_note, signature_block blocks, header_text "
        "or footer_text when relevant to the prompt or institutional context. "
        "Never force 'Radès, le ...' onto unrelated standalone documents.\n"
        "- Use schedules when target_class is supplied.\n"
        "- If an explicit_blueprint_id is supplied, it wins: route to that "
        "allowlisted blueprint using use_blueprint or modify_blueprint; never "
        "invent another id.\n"
        "- Never invent blueprint ids, courses, teachers, rooms or times.\n"
        "- Keep the summary concise and user-facing. Explain the routing "
        "decision briefly in explanation.\n"
        "- Respond in the language of the prompt (French by default).\n"
        "Return JSON matching the full schema including blueprint_confidence, "
        "new_doc and design_spec."
    )


def _apply_route_gate(
    decision: WordRouteDecision,
    *,
    prompt: str,
    target_class: str | None,
    catalog: list[dict[str, Any]] | None,
    schedules: list[dict[str, Any]] | None,
    explicit_blueprint_id: str | None,
    error: str | None = None,
) -> WordRouteDecision:
    """Enforce the allowlist and coerce unsafe routes to create_new."""
    allowed = {
        str(entry.get("id")): entry
        for entry in catalog or []
        if entry.get("id")
    }
    action = decision.action
    blueprint_id = decision.blueprint_id
    explanation = (decision.explanation or "").strip()

    if explicit_blueprint_id:
        if explicit_blueprint_id in allowed:
            if action == "create_new":
                action = "modify_blueprint" if decision.edits else "use_blueprint"
                decision.sections = []
                decision.design_spec = None
                explanation = (
                    f"{explanation} Explicit allowlisted blueprint selected; "
                    "routing to that template."
                ).strip()
            blueprint_id = explicit_blueprint_id
        else:
            action = "create_new"
            blueprint_id = None
            decision.sections = []
            explanation = (
                f"{explanation} The explicit blueprint is not allowlisted; "
                "creating a new document instead."
            ).strip()

    # Auto-mode hard gate: an unconfident or explicitly from-scratch request
    # must never silently fall back into a blueprint.
    if (
        explicit_blueprint_id is None
        and action in {"use_blueprint", "modify_blueprint"}
    ):
        if _has_from_scratch_intent(prompt):
            action = "create_new"
            blueprint_id = None
            explanation = (
                f"{explanation} The prompt explicitly requests a document from "
                "scratch; creating a new document instead."
            ).strip()
        elif decision.blueprint_confidence is None:
            action = "create_new"
            blueprint_id = None
            explanation = (
                f"{explanation} Blueprint confidence was not provided; "
                "creating a new document instead."
            ).strip()
        elif decision.blueprint_confidence < BLUEPRINT_CONFIDENCE_THRESHOLD:
            action = "create_new"
            blueprint_id = None
            explanation = (
                f"{explanation} Blueprint confidence "
                f"({decision.blueprint_confidence:.2f}) is below the "
                f"{BLUEPRINT_CONFIDENCE_THRESHOLD:.2f} threshold; creating a "
                "new document instead."
            ).strip()

    if action in {"use_blueprint", "modify_blueprint"}:
        if not blueprint_id or blueprint_id not in allowed:
            action = "create_new"
            blueprint_id = None
            explanation = (
                f"{explanation} No valid allowlisted blueprint was selected; "
                "creating a new document instead."
            ).strip()

    if action == "create_new":
        blueprint_id = None
        new_doc_data = (
            decision.new_doc.model_dump() if decision.new_doc is not None else None
        )
        if not new_doc_data or (
            not new_doc_data.get("sections") and not new_doc_data.get("tables")
        ):
            new_doc_data = _fallback_new_doc(
                prompt, prompt, target_class, schedules, error=error
            )
            decision.new_doc = WordNewDocument.model_validate(new_doc_data)

        decision.title = decision.title or str(new_doc_data.get("title") or "")

        design_data = (
            decision.design_spec.model_dump()
            if decision.design_spec is not None
            else None
        )
        if not design_data or not design_data.get("blocks"):
            design_data = _fallback_design_spec(
                new_doc_data, prompt, target_class, schedules
            )
            decision.design_spec = WordDesignSpec.model_validate(design_data)

        # Keep the legacy fields populated for older callers while the builder
        # renders the richer design_spec/new_doc structure.
        legacy_sections: list[dict[str, Any]] = []
        for section in new_doc_data.get("sections") or []:
            body_lines = [
                str(line).strip()
                for line in (
                    list(section.get("paragraphs") or [])
                    + list(section.get("bullets") or [])
                    + list(section.get("numbered_items") or [])
                )
                if str(line).strip()
            ]
            legacy_sections.append(
                {
                    "heading": section.get("heading"),
                    "body": "\n".join(body_lines),
                }
            )
        for table in new_doc_data.get("tables") or []:
            legacy_sections.append(
                {
                    "heading": table.get("title"),
                    "columns": table.get("headers") or [],
                    "rows": table.get("rows") or [],
                }
            )
        if legacy_sections:
            decision.sections = [
                WordSection.model_validate(section) for section in legacy_sections
            ]
        signature_block = new_doc_data.get("signature_block") or {}
        decision.signature_title = signature_block.get("title") or signature_block.get(
            "role"
        )
        decision.signature_name = signature_block.get("name")
    else:
        decision.new_doc = None
        decision.design_spec = None
        if action == "use_blueprint":
            decision.edits = [
                edit for edit in decision.edits if edit.action == "set_date"
            ]
            decision.sections = []

    if not decision.summary:
        if action == "use_blueprint":
            title = allowed.get(blueprint_id or "", {}).get("title", blueprint_id)
            decision.summary = f"Le modèle « {title} » a été utilisé."
        elif action == "modify_blueprint":
            title = allowed.get(blueprint_id or "", {}).get("title", blueprint_id)
            decision.summary = (
                f"Modifications ciblées appliquées au modèle « {title} »."
            )
        else:
            decision.summary = "Un nouveau document structuré a été préparé."

    if not explanation:
        explanation = {
            "use_blueprint": "Using an allowlisted template.",
            "modify_blueprint": "Applying targeted edits to an allowlisted template.",
            "create_new": "Creating a new structured document.",
        }[action]
    decision.explanation = explanation
    decision.action = action
    decision.blueprint_id = blueprint_id
    return decision


def _deduplicate(values: list[str]) -> list[str]:
    """Remove duplicates while preserving the original order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _model_rotation() -> list[str]:
    """Build the ordered model list used by Gemini routing attempts."""
    primary = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    configured = os.getenv("GEMINI_FALLBACK_MODELS", "").strip()
    fallbacks = (
        [item.strip() for item in configured.split(",") if item.strip()]
        if configured
        else list(DEFAULT_FALLBACK_MODELS)
    )
    return _deduplicate([primary, *fallbacks])


def _configured_timeout() -> float:
    """Per-attempt timeout in seconds (default: 30)."""
    raw = os.getenv("GEMINI_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    return min(max(value, 1.0), 120.0)


def _total_budget() -> float:
    """Overall wall-clock budget for one routed request (default: 75s)."""
    raw = os.getenv(
        "GEMINI_TOTAL_TIMEOUT_SECONDS", str(DEFAULT_TOTAL_BUDGET_SECONDS)
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TOTAL_BUDGET_SECONDS
    return min(max(value, 1.0), 600.0)


def _extract_status(exc: BaseException) -> int | None:
    """Best-effort HTTP/status extraction from google-genai exceptions."""
    for source in (exc, getattr(exc, "response", None)):
        if source is None:
            continue
        for attribute in ("status", "status_code", "code", "http_status"):
            value = getattr(source, attribute, None)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())

    match = re.search(r"\b([45]\d{2})\b", str(exc))
    return int(match.group(1)) if match else None


def _gemini_error_kind(exc: BaseException) -> str:
    """Classify a Gemini error as transient, not_found, or fatal."""
    status = _extract_status(exc)
    if status in TRANSIENT_STATUS_CODES:
        return "transient"
    if status in NOT_FOUND_STATUS_CODES:
        return "not_found"
    if status in FATAL_STATUS_CODES:
        return "fatal"

    text = str(exc).upper()
    if "NOT_FOUND" in text or "404" in text:
        return "not_found"
    if any(
        token in text
        for token in (
            "PERMISSION_DENIED",
            "UNAUTHENTICATED",
            "API_KEY_INVALID",
            "INVALID_ARGUMENT",
            "400",
            "401",
            "403",
        )
    ):
        return "fatal"
    if any(
        token in text
        for token in (
            "RESOURCE_EXHAUSTED",
            "UNAVAILABLE",
            "DEADLINE_EXCEEDED",
            "ABORTED",
            "429",
            "500",
            "502",
            "503",
            "504",
        )
    ):
        return "transient"
    return "fatal"


async def route_word_document(
    prompt: str,
    *,
    target_class: str | None = None,
    catalog: list[dict[str, Any]] | None = None,
    schedules: list[dict[str, Any]] | None = None,
    explicit_blueprint_id: str | None = None,
) -> dict[str, Any]:
    """Classify a free-form Word request and enforce the blueprint allowlist.

    Returns ``{"decision": {...}, "source": "gemini"|"fallback", "model": ...,
    "error": ...}``. Invalid blueprint selections are hard-gated to
    ``create_new`` with structured fallback sections.
    """
    catalog = catalog or []
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    def fallback_response(error_message: str) -> dict[str, Any]:
        fallback_error = error_message or "Gemini routing unavailable"
        decision = WordRouteDecision.model_validate(
            _fallback_word_route(
                prompt,
                target_class,
                catalog,
                schedules,
                explicit_blueprint_id,
                error=fallback_error,
            )
        )
        decision = _apply_route_gate(
            decision,
            prompt=prompt,
            target_class=target_class,
            catalog=catalog,
            schedules=schedules,
            explicit_blueprint_id=explicit_blueprint_id,
            error=fallback_error,
        )
        return {
            "decision": decision.model_dump(),
            "source": "fallback",
            "model": "fallback",
            "error": fallback_error,
        }

    if not api_key:
        return fallback_response("GEMINI_API_KEY is not configured")

    if genai is None or types is None:
        return fallback_response(
            f"google-genai is unavailable: {GENAI_IMPORT_ERROR}"
        )

    models = _model_rotation()
    per_attempt_timeout = _configured_timeout()
    budget = _total_budget()
    deadline = time.monotonic() + budget
    last_error = "All Gemini models failed"

    logger.info(
        "Gemini Word router starting: models=%s timeout=%.1fs budget=%.1fs",
        ",".join(models),
        per_attempt_timeout,
        budget,
    )

    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:
        last_error = f"Gemini client init failed: {type(exc).__name__}: {exc}"[:300]
        logger.warning("Gemini Word router could not create client: %s", last_error)
        return fallback_response(last_error)

    contents = _build_route_prompt(
        prompt,
        target_class,
        catalog,
        schedules,
        explicit_blueprint_id,
    )

    for model in models:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = f"Overall Gemini timeout budget ({budget:.1f}s) exhausted"
            logger.warning("Gemini Word router stopped: %s", last_error)
            break

        attempt_timeout = min(per_attempt_timeout, remaining)
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=(
                            "You are a precise Word document router and layout "
                            "designer. In Auto mode, use a blueprint only for a "
                            "clear allowlisted match with confidence >= 0.7; "
                            "otherwise create_new. Every create_new decision must "
                            "include a complete design_spec with ordered blocks and "
                            "topic-appropriate styling. Explicit from-scratch "
                            "requests always create a new document unless an "
                            "explicit blueprint override is given. Never force "
                            "institutional headers or dates onto unrelated topics. "
                            "Only select blueprint ids from the supplied allowlist. "
                            "Never invent data or blueprint references."
                        ),
                        temperature=0.1,
                        max_output_tokens=4096,
                        response_mime_type="application/json",
                        response_schema=WordRouteDecision,
                    ),
                ),
                timeout=attempt_timeout,
            )
        except asyncio.TimeoutError:
            last_error = f"{model}: timeout after {attempt_timeout:.1f}s"
            logger.warning(
                "Gemini Word router model %s timed out after %.1fs; rotating",
                model,
                attempt_timeout,
            )
            continue
        except Exception as exc:
            kind = _gemini_error_kind(exc)
            last_error = f"{model}: {type(exc).__name__}: {exc}"[:300]
            if kind == "not_found":
                logger.warning(
                    "Gemini Word router model %s unavailable (404/NOT_FOUND); rotating: %s",
                    model,
                    last_error,
                )
                continue
            if kind == "transient":
                logger.warning(
                    "Gemini Word router model %s transient failure; rotating: %s",
                    model,
                    last_error,
                )
                continue
            logger.warning(
                "Gemini Word router model %s non-transient failure: %s",
                model,
                last_error,
            )
            break

        try:
            parsed_response = getattr(response, "parsed", None)
            if isinstance(parsed_response, WordRouteDecision):
                decision = parsed_response
            elif isinstance(parsed_response, dict):
                decision = WordRouteDecision.model_validate(parsed_response)
            else:
                text = (getattr(response, "text", "") or "").strip()
                decision = WordRouteDecision.model_validate(_extract_json(text))

            decision = _apply_route_gate(
                decision,
                prompt=prompt,
                target_class=target_class,
                catalog=catalog,
                schedules=schedules,
                explicit_blueprint_id=explicit_blueprint_id,
            )
        except Exception as exc:
            last_error = (
                f"{model}: invalid routing response: "
                f"{type(exc).__name__}: {exc}"
            )[:300]
            logger.warning(
                "Gemini Word router model %s returned invalid data: %s",
                model,
                last_error,
            )
            break

        logger.info("Gemini Word router succeeded with model %s", model)
        return {
            "decision": decision.model_dump(),
            "source": "gemini",
            "model": model,
            "error": None,
        }

    return fallback_response(last_error)


def _schedule_rows(schedules: list[dict[str, Any]] | None) -> list[list[str]]:
    rows: list[list[str]] = []
    for entry in schedules or []:
        rows.append(
            [
                str(entry.get("day") or ""),
                str(entry.get("time_slot") or ""),
                str(entry.get("subject") or ""),
                str(entry.get("teacher") or ""),
                str(entry.get("room") or ""),
                str(entry.get("week") or ""),
            ]
        )
    return rows


def _fallback_word_plan(
    subject: str,
    instructions: str | None,
    target_class: str | None,
    blueprint: dict[str, Any] | None,
    schedules: list[dict[str, Any]] | None,
    error: str | None = None,
    *,
    include_new_doc: bool = True,
) -> dict[str, Any]:
    """Build a safe, deterministic plan without calling Gemini."""
    edits = _parse_instruction_edits(instructions)
    sections: list[dict[str, Any]] = []

    if not blueprint:
        info_lines = ["Département Technologies de l'Informatique — ISET Radès"]
        sections.append({"heading": "Informations", "body": "\n".join(info_lines)})

        rows = _schedule_rows(schedules)
        if rows:
            sections.append(
                {
                    "heading": "Emploi du temps",
                    "columns": [
                        "Jour",
                        "Horaire",
                        "Matière",
                        "Enseignant",
                        "Salle",
                        "Semaine",
                    ],
                    "rows": rows,
                }
            )

        if instructions and instructions.strip():
            sections.append({"heading": "Notes", "body": instructions.strip()})
    elif schedules:
        request_text = (instructions or "").lower()
        wants_timetable = any(
            marker in request_text
            for marker in (
                "emploi du temps",
                "horaire",
                "planning",
                "schedule",
                "timetable",
            )
        )
        if wants_timetable:
            sections.append(
                {
                    "heading": "Emploi du temps",
                    "columns": [
                        "Jour",
                        "Horaire",
                        "Matière",
                        "Enseignant",
                        "Salle",
                        "Semaine",
                    ],
                    "rows": _schedule_rows(schedules),
                }
            )

    summary = (
        f"Plan local pour « {subject} »."
        if not blueprint
        else f"Plan local de modifications ciblées pour « {blueprint.get('filename', 'le modèle')} »."
    )

    return {
        "title": subject,
        "summary": summary,
        "signature_title": None,
        "signature_name": None,
        "edits": edits,
        "sections": sections,
        "new_doc": (
            _fallback_new_doc(subject, instructions, target_class, schedules, error)
            if include_new_doc and not blueprint
            else None
        ),
        "_fallback_error": error,
    }


def _build_word_prompt(
    subject: str,
    instructions: str | None,
    target_class: str | None,
    blueprint: dict[str, Any] | None,
    schedules: list[dict[str, Any]] | None,
) -> str:
    payload = {
        "today": date.today().isoformat(),
        "mode": "blueprint_edit" if blueprint else "standalone",
        "subject": subject,
        "target_class": target_class,
        "instructions": instructions or "",
        "blueprint": blueprint,
        "schedules": schedules or [],
    }
    return (
        "You are the Word document planner for the IT department secretary at "
        "ISET Radès. Build a precise plan for the requested document.\n\n"
        f"Request data:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "Rules:\n"
        "- If a blueprint is supplied, preserve it. Return only the targeted "
        "edits needed by the subject/instructions; never rewrite the whole file.\n"
        "- For a 'Liste des Besoins' layout, use set_date for the date line and "
        "add_item/update_item/remove_item for the N° | Désignation | Quantité "
        "table. Use set_signature only when the signatory changes.\n"
        "- When a blueprint contains several historical sections, omit "
        "table_index so the most recent department table is targeted, unless the "
        "user explicitly names a table.\n"
        "- Use set_table_header for column labels, set_title/set_header for the "
        "document heading, set_paragraph for an exact existing paragraph, and "
        "insert_section to append new content.\n"
        "- For a standalone document, return a title and structured sections "
        "with headings, body text and optional tables.\n"
        "- If schedules for target_class are present, use them to build a useful "
        "timetable table; never invent courses, teachers, rooms or times.\n"
        "- Keep quantities and designations exactly as requested. Do not invent "
        "items that are not in the instructions, blueprint or schedules.\n"
        "- Respond in the language of the request (French by default).\n"
        'Return JSON matching the schema: title, summary, signature_title, '
        "signature_name, edits[], sections[]."
    )


async def plan_word_document(
    subject: str,
    instructions: str | None = None,
    target_class: str | None = None,
    blueprint: dict[str, Any] | None = None,
    schedules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask Gemini for a structured Word plan, with a deterministic fallback.

    Returns ``{"plan": {...}, "source": "gemini"|"fallback", "model": ...,
    "error": ...}``. This function never raises: a planner failure degrades to
    a local plan so document generation remains available.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    if not api_key:
        plan = _fallback_word_plan(
            subject,
            instructions,
            target_class,
            blueprint,
            schedules,
            error="GEMINI_API_KEY is not configured",
        )
        plan_error = plan.pop("_fallback_error", None)
        return {
            "plan": plan,
            "source": "fallback",
            "model": None,
            "error": plan_error,
        }
    if genai is None or types is None:
        plan = _fallback_word_plan(
            subject,
            instructions,
            target_class,
            blueprint,
            schedules,
            error=f"google-genai is unavailable: {GENAI_IMPORT_ERROR}",
        )
        plan_error = plan.pop("_fallback_error", None)
        return {
            "plan": plan,
            "source": "fallback",
            "model": None,
            "error": plan_error,
        }

    timeout = 20.0
    try:
        timeout = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "20"))
        timeout = min(max(timeout, 5.0), 60.0)

        client = genai.Client(api_key=api_key)
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=_build_word_prompt(
                    subject,
                    instructions,
                    target_class,
                    blueprint,
                    schedules,
                ),
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You are a conservative Word document editor. Preserve "
                        "existing content and formatting, and only return edits "
                        "explicitly supported by the request. Never invent data."
                    ),
                    temperature=0.15,
                    max_output_tokens=4096,
                    response_mime_type="application/json",
                    response_schema=WordPlan,
                ),
            ),
            timeout=timeout,
        )

        parsed_response = getattr(response, "parsed", None)
        if isinstance(parsed_response, WordPlan):
            plan = parsed_response
        elif isinstance(parsed_response, dict):
            plan = WordPlan.model_validate(parsed_response)
        else:
            text = (getattr(response, "text", "") or "").strip()
            plan = WordPlan.model_validate(_extract_json(text))

        return {
            "plan": plan.model_dump(),
            "source": "gemini",
            "model": model,
            "error": None,
        }
    except asyncio.TimeoutError:
        logger.warning("Gemini Word planner timed out after %.1fs", timeout)
        error = "Gemini Word planner timed out"
    except Exception as exc:
        logger.warning("Gemini Word planner failed: %s", exc)
        error = f"{type(exc).__name__}: {exc}"[:300]

    plan = _fallback_word_plan(
        subject,
        instructions,
        target_class,
        blueprint,
        schedules,
        error=error,
    )
    plan_error = plan.pop("_fallback_error", None)
    return {
        "plan": plan,
        "source": "fallback",
        "model": None,
        "error": plan_error,
    }

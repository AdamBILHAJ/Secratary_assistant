"""Word (.docx) blueprint management and prompt-driven generation endpoints."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from docx import Document
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.doc_generator import (
    BLUEPRINT_DIR,
    DOCX_MEDIA_TYPE,
    BlueprintNotAllowedError,
    BlueprintNotFoundError,
    DocumentGenerationError,
    InvalidBlueprintError,
    execute_word_decision,
    list_blueprints,
    register_uploaded_blueprint,
    resolve_blueprint_entry,
)
from services.gemini_service import route_word_document
from services.schedule_store import ScheduleStoreError, fetch_class_schedules

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class DocumentGenerateRequest(BaseModel):
    """Request body for ``POST /api/documents/generate``.

    ``prompt`` is the preferred free-form input. ``subject``/``instructions``
    remain supported for backwards compatibility with Part 3 clients.
    """

    doc_type: Literal["word", "excel"] = Field(
        default="word",
        description="Document type. Word is implemented; Excel is not available yet.",
        examples=["word"],
    )
    prompt: str | None = Field(
        default=None,
        description="Free-form request used for intent routing and generation.",
        examples=[
            "Add 10 HDMI cables and remove scotch from the Liste des Besoins, date 22/11/2026"
        ],
    )
    subject: str | None = Field(
        default=None,
        description="Backwards-compatible title/subject when prompt is not supplied.",
        examples=["Liste des besoins du département TI"],
    )
    target_class: str | None = Field(
        default=None,
        description="Optional class name used to enrich the document with schedules.",
        examples=["L1-TI-1"],
    )
    instructions: str | None = Field(
        default=None,
        description=(
            "Backwards-compatible targeted modifications or content requirements. "
            "Used as part of the routing prompt when prompt is absent."
        ),
        examples=["Add 10 HDMI cables and remove Multiprise. Date: 22/11/2026"],
    )
    blueprint_id: str | None = Field(
        default=None,
        description="Optional allowlisted blueprint id override.",
        examples=["liste-besoins-sept-2024"],
    )
    blueprint_name: str | None = Field(
        default=None,
        description=(
            "Backwards-compatible allowlisted blueprint filename or stem override."
        ),
        examples=["Liste des Besoins sept 2024.docx"],
    )
    output_name: str | None = Field(
        default=None,
        description="Optional output filename (without or with .docx).",
        examples=["Liste des Besoins - novembre 2026.docx"],
    )


class BlueprintUploadResponse(BaseModel):
    status: str
    id: str
    name: str
    filename: str
    title: str
    description: str
    location: str
    size_bytes: int
    overwritten: bool


def _safe_output_filename(value: str | None, fallback: str) -> str:
    text = (value or "").strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:150].strip(" .")
    if not text:
        text = "document"
    if not text.lower().endswith(".docx"):
        text += ".docx"
    return text


def _sanitize_upload_filename(filename: str | None) -> str:
    name = Path(filename or "").name.strip()
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="A .docx filename is required.")
    if not name.lower().endswith(".docx"):
        raise HTTPException(
            status_code=400,
            detail="Only .docx files are accepted for Word blueprints.",
        )
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or not name.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Invalid .docx filename.")
    # Store with the canonical lowercase extension so listing/resolution is
    # consistent even when the client uploads "Template.DOCX".
    name = f"{name[:-5]}.docx"[:180]
    return name


def _unique_upload_path(filename: str, overwrite: bool) -> Path:
    BLUEPRINT_DIR.mkdir(parents=True, exist_ok=True)
    target = BLUEPRINT_DIR / filename
    if overwrite or not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = BLUEPRINT_DIR / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(
        status_code=400,
        detail="Too many uploaded blueprints with the same name.",
    )


def _validate_docx_bytes(content: bytes) -> None:
    """Reject files that are not readable Word documents."""
    try:
        document = Document(io.BytesIO(content))
        _ = document.paragraphs
        _ = document.tables
    except Exception as exc:
        raise ValueError(f"not a valid .docx file: {exc}") from exc


def _content_disposition(filename: str) -> str:
    fallback = filename.encode("ascii", "ignore").decode("ascii") or "document.docx"
    fallback = fallback.replace('"', "'").replace("\\", "_")
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _encoded_header(value: str | None, *, limit: int = 600) -> str:
    """Percent-encode a UTF-8 header value so it stays Latin-1 safe."""
    return quote((value or "")[:limit], safe="")


async def _load_schedules(target_class: str | None) -> list[dict[str, Any]]:
    if not target_class or not target_class.strip():
        return []
    try:
        return await fetch_class_schedules(target_class.strip())
    except ScheduleStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "MongoDB schedules are unavailable, so the document cannot be "
                f"enriched for class '{target_class}': {exc}"
            ),
        ) from exc


async def _resolve_override(payload: DocumentGenerateRequest) -> dict[str, Any] | None:
    """Validate an explicit blueprint override against the allowlist."""
    references = [
        reference.strip()
        for reference in (payload.blueprint_id, payload.blueprint_name)
        if reference and reference.strip()
    ]
    if not references:
        return None

    entries: list[dict[str, Any]] = []
    for reference in references:
        try:
            entry = await asyncio.to_thread(resolve_blueprint_entry, reference)
        except BlueprintNotAllowedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BlueprintNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"Blueprint '{reference}' was not found in the allowlist.",
            )
        entries.append(entry)

    identifiers = {str(entry["id"]) for entry in entries}
    if len(identifiers) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "blueprint_id and blueprint_name refer to different allowlisted "
                "templates."
            ),
        )
    return entries[0]


def _compose_prompt(payload: DocumentGenerateRequest) -> str:
    prompt = (payload.prompt or "").strip()
    if prompt:
        return prompt

    parts = [
        part.strip()
        for part in (payload.subject, payload.instructions)
        if part and part.strip()
    ]
    if not parts:
        raise HTTPException(
            status_code=400,
            detail="Provide a prompt (or subject/instructions for compatibility).",
        )
    return "\n".join(parts)


@router.get("/blueprints", summary="List allowlisted Word (.docx) blueprints")
async def get_blueprints() -> dict[str, Any]:
    """Return the explicit allowlist of Word templates available for routing."""
    blueprints = await asyncio.to_thread(list_blueprints)
    return {
        "count": len(blueprints),
        "allowlisted": True,
        "blueprints": blueprints,
        "upload_directory": "backend/blueprints",
    }


@router.post(
    "/blueprints/upload",
    response_model=BlueprintUploadResponse,
    status_code=201,
    summary="Upload a custom Word (.docx) blueprint",
)
async def upload_blueprint(
    file: UploadFile = File(..., description="A .docx Word template."),
    overwrite: bool = Query(
        default=False,
        description="Replace an existing blueprint with the same filename.",
    ),
) -> BlueprintUploadResponse:
    """Validate, store and explicitly allowlist a custom template."""
    filename = _sanitize_upload_filename(file.filename)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Blueprint exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )

    try:
        await asyncio.to_thread(_validate_docx_bytes, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target = _unique_upload_path(filename, overwrite)
    overwritten = overwrite and target.exists()
    try:
        await asyncio.to_thread(target.write_bytes, content)
    except OSError as exc:
        logger.exception("Could not store uploaded blueprint")
        raise HTTPException(
            status_code=500, detail=f"Could not store the blueprint: {exc}"
        ) from exc

    try:
        entry = await asyncio.to_thread(
            register_uploaded_blueprint,
            target.name,
            title=target.stem,
            description=f"Modèle Word importé et enregistré dans l'allowlist ({target.name}).",
        )
    except (OSError, ValueError) as exc:
        logger.exception("Could not register uploaded blueprint")
        raise HTTPException(
            status_code=500, detail=f"Could not register the blueprint: {exc}"
        ) from exc

    return BlueprintUploadResponse(
        status="uploaded",
        id=str(entry["id"]),
        name=target.stem,
        filename=target.name,
        title=str(entry["title"]),
        description=str(entry["description"]),
        location="blueprints",
        size_bytes=len(content),
        overwritten=overwritten,
    )


@router.post(
    "/generate",
    summary="Generate a Word (.docx) document from a prompt",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {DOCX_MEDIA_TYPE: {}},
            "description": "Generated Word document.",
        },
        400: {"description": "Invalid request, Excel requested, or non-allowlisted blueprint."},
        404: {"description": "Allowlisted blueprint file was not found."},
        503: {"description": "MongoDB or the document router is unavailable."},
    },
)
async def generate_document(payload: DocumentGenerateRequest) -> StreamingResponse:
    """Route a free-form prompt to a blueprint or a clean structured document."""
    if payload.doc_type == "excel":
        raise HTTPException(
            status_code=400,
            detail=(
                "Excel generation is not available yet. Use doc_type='word' "
                "for Word (.docx) generation."
            ),
        )

    prompt = _compose_prompt(payload)
    target_class = (payload.target_class or "").strip() or None

    explicit_entry = await _resolve_override(payload)
    explicit_blueprint_id = (
        str(explicit_entry["id"]) if explicit_entry is not None else None
    )

    schedules = await _load_schedules(target_class)
    catalog = await asyncio.to_thread(list_blueprints)

    try:
        routing = await route_word_document(
            prompt,
            target_class=target_class,
            catalog=catalog,
            schedules=schedules,
            explicit_blueprint_id=explicit_blueprint_id,
        )
    except Exception as exc:  # router should degrade to fallback, but be safe
        logger.exception("Word router failed unexpectedly")
        raise HTTPException(
            status_code=503,
            detail=f"The Word document router is unavailable: {exc}",
        ) from exc

    decision = routing["decision"]

    try:
        result = await asyncio.to_thread(
            execute_word_decision,
            decision,
            prompt=prompt,
            target_class=target_class,
            schedules=schedules,
        )
    except BlueprintNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BlueprintNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidBlueprintError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DocumentGenerationError as exc:
        logger.exception("Word generation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected Word generation failure")
        raise HTTPException(
            status_code=500, detail=f"Word generation failed: {exc}"
        ) from exc

    content: bytes = result["content"]
    if not content:
        raise HTTPException(
            status_code=500, detail="Word generation produced an empty file."
        )

    action = str(result.get("action") or decision.get("action") or "create_new")
    subject = (payload.subject or "").strip()
    decision_title = str(decision.get("title") or "").strip()
    blueprint_title = str(result.get("blueprint_title") or "").strip()

    if action in {"use_blueprint", "modify_blueprint"}:
        base_name = decision_title or blueprint_title or subject or "document"
        if subject and subject.casefold() not in base_name.casefold():
            base_name = f"{base_name} - {subject}"
    else:
        base_name = decision_title or subject or "document"

    filename = _safe_output_filename(payload.output_name, base_name)
    applied = result.get("applied_edits") or []
    blueprint_used = str(result.get("blueprint_id") or "none")

    headers = {
        "Content-Disposition": _content_disposition(filename),
        "Content-Length": str(len(content)),
        "X-Doc-Action": action,
        "X-Blueprint-Used": blueprint_used,
        "X-AI-Source": str(routing.get("source") or "fallback"),
        "X-AI-Model": str(routing.get("model") or "fallback"),
        "X-Applied-Edits": str(len(applied)),
        "X-Doc-Summary": _encoded_header(str(decision.get("summary") or "")),
        "X-Doc-Explanation": _encoded_header(
            str(decision.get("explanation") or "")
        ),
    }

    return StreamingResponse(
        io.BytesIO(content),
        media_type=DOCX_MEDIA_TYPE,
        headers=headers,
    )

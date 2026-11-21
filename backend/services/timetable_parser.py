"""PDF timetable parser for ISET Radès class schedules.

The exported PDF (Index Education) draws each timetable as a 6x6 grid:

* six time-slot columns (08:30 -> 18:30),
* six day rows (lundi -> samedi),
* every course block is a filled rectangle containing the subject, teacher,
  room and (optionally) an alternating-week badge (Q1/Q2).

The text layer is not in reading order, so the parser relies on geometry
(rectangles + grid lines) rather than ``page.extract_text()`` alone.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pdfplumber

logger = logging.getLogger(__name__)

PDF_FILENAME = "Emploi_du_temps_Classes_Sem2.pdf"
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent

DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi")

# Column order in the PDF header.
TIME_SLOTS: tuple[dict[str, str], ...] = (
    {"start": "08:30", "end": "10:00", "label": "08:30-10:00"},
    {"start": "10:15", "end": "11:45", "label": "10:15-11:45"},
    {"start": "12:00", "end": "13:30", "label": "12:00-13:30"},
    {"start": "13:30", "end": "15:00", "label": "13:30-15:00"},
    {"start": "15:15", "end": "16:45", "label": "15:15-16:45"},
    {"start": "17:00", "end": "18:30", "label": "17:00-18:30"},
)
TIME_SLOT_LABELS = tuple(slot["label"] for slot in TIME_SLOTS)

# Known department room identifiers. Q1/Q2 are alternating-week badges, not
# rooms, and G1/G2 are derived groups, so none of them belong here.
DEFAULT_ROOMS = (
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "I6",
    "I7",
    "I8",
    "I9",
    "I10",
    "I11",
    "I12",
    "I11(SP1)",
    "I12(AB17)",
    "TI1",
    "TI2",
    "C1",
    "C9",
    "E2",
    "E4",
    "G5",
    "G6",
    "11",
    "12",
    "ABS",
)

# Q1/Q2 in a course block is the alternating-week badge.
_WEEK_TOKEN = re.compile(r"^Q[12]$", re.IGNORECASE)
# Department room codes: I6, TI1, C9, E2, G6, GE10, GER19, 11, ABS, ...
_ROOM_TOKEN = re.compile(
    r"^(?:[A-Z]{1,3}\d{1,3}(?:\([A-Z0-9]+\))?|\d{1,3}|ABS)$",
    re.IGNORECASE,
)
# MATH1/MATH2 are regular teacher codes in this PDF, not rooms.
_TEACHER_CODE = re.compile(r"^MATH\d{1,3}$", re.IGNORECASE)
_CLASS_RE = re.compile(r"classe\s+([A-Za-z0-9][A-Za-z0-9_\-/]*)", re.IGNORECASE)
_WEEK_RE = re.compile(r"Semaine\s+du\s+(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
_GENERATED_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})")


class TimetableError(RuntimeError):
    """Base class for timetable parsing failures."""


class TimetableFileNotFound(TimetableError):
    """Raised when no source PDF can be located."""


class TimetableParseError(TimetableError):
    """Raised when the PDF cannot be read or understood."""


def _absolute_candidates(values: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for candidate in values:
        if candidate.is_absolute():
            resolved.append(candidate.resolve())
            continue
        for base in (Path.cwd(), BACKEND_DIR, PROJECT_ROOT):
            resolved.append((base / candidate).resolve())

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in resolved:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def resolve_pdf_path(pdf_path: str | Path | None = None) -> Path:
    """Locate the timetable PDF.

    Precedence: explicit argument -> ``TIMETABLE_PDF_PATH`` -> repository
    root -> backend directory -> current working directory. An explicitly
    configured path that does not exist is an error (no silent fallback).
    """
    explicit: list[Path] = []
    if pdf_path:
        explicit.append(Path(pdf_path).expanduser())
    env_path = os.getenv("TIMETABLE_PDF_PATH", "").strip()
    if env_path:
        explicit.append(Path(env_path).expanduser())

    if explicit:
        candidates = _absolute_candidates(explicit)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = "\n".join(f"  - {path}" for path in candidates)
        raise TimetableFileNotFound(
            "The configured timetable PDF was not found. Check "
            "TIMETABLE_PDF_PATH.\nSearched:\n" + searched
        )

    candidates = _absolute_candidates(
        [Path(PDF_FILENAME), Path(BACKEND_DIR) / PDF_FILENAME]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = "\n".join(f"  - {path}" for path in candidates)
    raise TimetableFileNotFound(
        f"Timetable PDF '{PDF_FILENAME}' was not found. Set TIMETABLE_PDF_PATH "
        f"or place the file in the project root.\nSearched:\n{searched}"
    )


def _decode_doubled_token(token: str) -> str:
    """Decode Index Education's 'EEmmppllooii' style title text."""
    if len(token) >= 4 and len(token) % 2 == 0:
        even = token[0::2]
        if even == token[1::2] and any(char.isalpha() for char in even):
            return even
    return token


def _clean_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\u2019", "'")
    return re.sub(r"\s+", " ", value).strip()


def _clean_subject(value: str) -> str:
    """Repair a text-layer glitch where one cell's overflow is glued to the next.

    Example: ``d'emploTechnique de Recherche d'emplo`` -> ``Technique de
    Recherche d'emplo``. The leading fragment is only dropped when it is also
    a suffix of the remaining (properly capitalised) title.
    """
    text = _clean_text(value)
    for index in range(1, len(text)):
        prefix = text[:index]
        remaining = text[index:]
        if prefix and remaining and remaining[0].isupper() and remaining.endswith(prefix):
            return remaining.strip()
    return text


def _line_text(line_words: list[dict[str, Any]]) -> str:
    ordered = sorted(line_words, key=lambda word: word["x0"])
    return _clean_text(" ".join(word["text"] for word in ordered))


def _group_words_into_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group words sharing a baseline into visual text lines."""
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        if lines and abs(word["top"] - lines[-1]["top"]) <= 2:
            lines[-1]["words"].append(word)
        else:
            lines.append({"top": word["top"], "words": [word]})

    for line in lines:
        line["text"] = _line_text(line["words"])
        line["x0"] = min(word["x0"] for word in line["words"])
        line["x1"] = max(word["x1"] for word in line["words"])
        line["bottom"] = max(word["bottom"] for word in line["words"])
        line["italic"] = any(
            "oblique" in str(word.get("fontname", "")).lower()
            or "italic" in str(word.get("fontname", "")).lower()
            for word in line["words"]
        )
    return lines


def _dedupe_sorted(values: list[float], tolerance: float = 1.0) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
    return result


def _extract_grid(page: pdfplumber.page.Page) -> tuple[list[float], list[float]]:
    """Return vertical column edges and horizontal day-row edges."""
    vertical = _dedupe_sorted(
        [
            float(line["x0"])
            for line in page.lines
            if abs(line["x1"] - line["x0"]) < 1 and line["top"] < 70 and line["bottom"] > 50
        ]
    )
    horizontal = _dedupe_sorted(
        [
            float(line["top"])
            for line in page.lines
            if abs(line["bottom"] - line["top"]) < 1 and line["x0"] < 32 and line["x1"] > 55
        ]
    )

    if len(vertical) != len(TIME_SLOTS) + 1:
        raise TimetableParseError(
            f"unexpected timetable grid: found {len(vertical)} column edges, "
            f"expected {len(TIME_SLOTS) + 1}"
        )
    if len(horizontal) != len(DAYS) + 1:
        raise TimetableParseError(
            f"unexpected timetable grid: found {len(horizontal)} row edges, "
            f"expected {len(DAYS) + 1}"
        )
    return vertical, horizontal


def _extract_class_name(title: str) -> str | None:
    match = _CLASS_RE.search(title)
    if not match:
        return None
    return match.group(1).strip().rstrip("-")


def _page_title(page: pdfplumber.page.Page) -> str:
    words = page.extract_words(extra_attrs=["size"])
    title_words = [
        word
        for word in words
        if word.get("size", 0) >= 11 and word["top"] < 55
    ]
    title_words.sort(key=lambda word: word["x0"])
    return _clean_text(" ".join(_decode_doubled_token(word["text"]) for word in title_words))


def _day_labels(page: pdfplumber.page.Page, day_column_x: float) -> list[str]:
    labels: list[tuple[float, str]] = []
    for word in page.extract_words():
        normalized = unicodedata.normalize("NFKD", word["text"].lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        if normalized in DAYS and word["x0"] < day_column_x + 2:
            labels.append((word["top"], normalized))
    labels.sort()
    if [label for _, label in labels] == list(DAYS):
        return [label for _, label in labels]
    return list(DAYS)


def _column_span(rect: dict[str, Any], vertical: list[float]) -> tuple[int, int]:
    """Map a course rectangle to one or more time-slot column indexes."""
    covered: list[int] = []
    for index in range(len(vertical) - 1):
        overlap = min(rect["x1"], vertical[index + 1]) - max(rect["x0"], vertical[index])
        if overlap > 10:
            covered.append(index)
    if not covered:
        raise TimetableParseError("course block does not overlap any time-slot column")
    return covered[0], covered[-1]


def _row_index(rect: dict[str, Any], horizontal: list[float]) -> int:
    center = (rect["top"] + rect["bottom"]) / 2
    for index in range(len(horizontal) - 1):
        if horizontal[index] <= center <= horizontal[index + 1]:
            return index
    raise TimetableParseError("course block does not overlap any day row")


def _extract_week_and_room(
    lines: list[dict[str, Any]],
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Split a course block into ``(week, room, remaining_lines)``.

    ``Q1``/``Q2`` are alternating-week badges, never rooms. Every other
    department room identifier (``I6``, ``TI1``, ``C9``, ``E2``, ``G6``,
    ``ABS``, numeric rooms, ...) maps directly to the ``room`` field. Groups
    are derived from the week later.
    """
    week_line: dict[str, Any] | None = None
    for line in lines:
        if _WEEK_TOKEN.fullmatch(line["text"].strip()):
            week_line = line
            break

    week = week_line["text"].strip().upper() if week_line else None
    candidates = [line for line in lines if line is not week_line]

    room_line: dict[str, Any] | None = None
    for line in reversed(candidates):
        text = line["text"].strip()
        if _TEACHER_CODE.fullmatch(text):
            continue
        if _ROOM_TOKEN.fullmatch(text):
            room_line = line
            break

    room = room_line["text"].strip().upper() if room_line else None
    remaining = [line for line in candidates if line is not room_line]
    return week, room, remaining


def _entry_key(
    class_name: str,
    day: str,
    slots: list[str],
    subject: str,
    teacher: str | None,
    room: str | None,
    week: str | None,
) -> str:
    raw = "|".join(
        [
            class_name,
            day,
            ",".join(slots),
            subject,
            teacher or "",
            room or "",
            week or "",
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _parse_course_rect(
    rect: dict[str, Any],
    words: list[dict[str, Any]],
    vertical: list[float],
    horizontal: list[float],
    day_labels: list[str],
    class_name: str,
    page_number: int,
    source_file: str,
    week_start: str | None,
) -> dict[str, Any] | None:
    inside = [
        word
        for word in words
        if rect["x0"] - 1 <= (word["x0"] + word["x1"]) / 2 <= rect["x1"] + 1
        and rect["top"] - 1 <= (word["top"] + word["bottom"]) / 2 <= rect["bottom"] + 1
    ]
    if not inside:
        return None

    lines = [line for line in _group_words_into_lines(inside) if line["text"]]
    if not lines:
        return None

    week, room, remaining = _extract_week_and_room(lines)

    teacher: str | None = None
    subject = ""
    if len(remaining) >= 2:
        teacher = remaining[-1]["text"]
        subject = _clean_subject(" ".join(line["text"] for line in remaining[:-1]))
    elif len(remaining) == 1:
        subject = _clean_subject(remaining[0]["text"])
    else:
        return None

    if not subject:
        return None

    group = {"Q1": "G1", "Q2": "G2"}.get(week)

    first_column, last_column = _column_span(rect, vertical)
    selected_slots = list(TIME_SLOTS[first_column : last_column + 1])
    slots = [slot["label"] for slot in selected_slots]
    time_slot = f"{selected_slots[0]['start']}-{selected_slots[-1]['end']}"

    row = _row_index(rect, horizontal)
    day = day_labels[row] if row < len(day_labels) else DAYS[row]

    return {
        "entry_key": _entry_key(class_name, day, slots, subject, teacher, room, week),
        "class_name": class_name,
        "day": day,
        "time_slot": time_slot,
        "start_time": selected_slots[0]["start"],
        "end_time": selected_slots[-1]["end"],
        "slots": slots,
        "subject": subject,
        "teacher": teacher,
        "room": room,
        "week": week,
        "group": group,
        "page": page_number,
        "week_start": week_start,
        "source_file": source_file,
    }


def _page_events(
    page: pdfplumber.page.Page,
    vertical: list[float],
    horizontal: list[float],
    day_labels: list[str],
    class_name: str,
    page_number: int,
    source_file: str,
    week_start: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    words = page.extract_words(extra_attrs=["size", "fontname"])
    entries: list[dict[str, Any]] = []
    seen_boxes: set[tuple[float, float, float, float]] = set()

    for rect in page.rects:
        width = rect["x1"] - rect["x0"]
        height = rect["bottom"] - rect["top"]

        # Exclude page background, header cells and other non-course shapes.
        if width < 35 or height < 18:
            continue
        if width > (vertical[-1] - vertical[0]) * 1.01:
            continue
        if height > (horizontal[-1] - horizontal[0]) * 1.01:
            continue
        if rect["x1"] <= vertical[0] + 1 or rect["x0"] >= vertical[-1] - 1:
            continue
        if rect["bottom"] <= horizontal[0] + 1 or rect["top"] >= horizontal[-1] - 1:
            continue

        box = (
            round(rect["x0"], 1),
            round(rect["top"], 1),
            round(rect["x1"], 1),
            round(rect["bottom"], 1),
        )
        if box in seen_boxes:
            continue
        seen_boxes.add(box)

        try:
            entry = _parse_course_rect(
                rect,
                words,
                vertical,
                horizontal,
                day_labels,
                class_name,
                page_number,
                source_file,
                week_start,
            )
        except TimetableParseError as exc:
            warnings.append(f"page {page_number}: skipped malformed course block: {exc}")
            continue
        if entry:
            entries.append(entry)

    # Fallback for PDFs where the filled rectangles are missing: group words
    # by grid cell and then by vertical gaps.
    if not entries:
        entries = _cell_fallback_events(
            page,
            vertical,
            horizontal,
            day_labels,
            class_name,
            page_number,
            source_file,
            week_start,
            warnings,
        )

    return entries, warnings


def _cell_fallback_events(
    page: pdfplumber.page.Page,
    vertical: list[float],
    horizontal: list[float],
    day_labels: list[str],
    class_name: str,
    page_number: int,
    source_file: str,
    week_start: str | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Best-effort extraction when course rectangles are unavailable."""
    words = page.extract_words(extra_attrs=["fontname"])
    entries: list[dict[str, Any]] = []

    for row in range(len(horizontal) - 1):
        for column in range(len(vertical) - 1):
            cell = {
                "x0": vertical[column],
                "top": horizontal[row],
                "x1": vertical[column + 1],
                "bottom": horizontal[row + 1],
            }
            cell_words = [
                word
                for word in words
                if cell["x0"] - 1 <= (word["x0"] + word["x1"]) / 2 <= cell["x1"] + 1
                and cell["top"] - 1 <= (word["top"] + word["bottom"]) / 2 <= cell["bottom"] + 1
            ]
            if len(cell_words) < 3:
                continue

            # Split parallel courses in the same cell using larger vertical gaps.
            blocks: list[list[dict[str, Any]]] = []
            for word in sorted(cell_words, key=lambda item: item["top"]):
                if blocks and word["top"] - max(item["bottom"] for item in blocks[-1]) < 8:
                    blocks[-1].append(word)
                else:
                    blocks.append([word])

            for block in blocks:
                block_rect = {
                    "x0": min(word["x0"] for word in block),
                    "top": min(word["top"] for word in block),
                    "x1": max(word["x1"] for word in block),
                    "bottom": max(word["bottom"] for word in block),
                }
                try:
                    entry = _parse_course_rect(
                        block_rect,
                        block,
                        vertical,
                        horizontal,
                        day_labels,
                        class_name,
                        page_number,
                        source_file,
                        week_start,
                    )
                except TimetableParseError as exc:
                    warnings.append(
                        f"page {page_number}: fallback parse skipped a block: {exc}"
                    )
                    continue
                if entry:
                    entries.append(entry)

    return entries


def _parse_metadata(page: pdfplumber.page.Page) -> tuple[str, str | None, str | None]:
    text = page.extract_text() or ""
    week_start: str | None = None
    week_match = _WEEK_RE.search(text)
    if week_match:
        try:
            parsed = datetime.strptime(week_match.group(1), "%d/%m/%Y")
            week_start = parsed.date().isoformat()
        except ValueError:
            week_start = None

    generated_at: str | None = None
    generated_match = _GENERATED_RE.search(text)
    if generated_match:
        generated_at = f"{generated_match.group(1)} {generated_match.group(2)}"

    department = "Département TI - ISET Radès"
    department_match = re.search(
        r"Département\s+([A-Za-zÀ-ÿ]+)\s+-\s+(ISET\s+[A-Za-zÀ-ÿ]+)", text
    )
    if department_match:
        department = _clean_text(
            f"Département {department_match.group(1)} - {department_match.group(2)}"
        )

    return department, week_start, generated_at


def parse_timetable_pdf(pdf_path: str | Path | None = None) -> dict[str, Any]:
    """Parse the timetable PDF and return structured JSON-ready data.

    Raises:
        TimetableFileNotFound: no PDF at the configured/default location.
        TimetableParseError: the PDF cannot be opened or parsed.
    """
    path = resolve_pdf_path(pdf_path)
    source_file = path.name
    warnings: list[str] = []
    classes: list[dict[str, Any]] = []
    department = "Département TI - ISET Radès"
    week_start: str | None = None
    generated_at: str | None = None

    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:  # pdfplumber/pdfminer raise several exception types
        raise TimetableParseError(f"could not open '{path.name}': {exc}") from exc

    with pdf:
        page_count = len(pdf.pages)
        if page_count == 0:
            raise TimetableParseError(f"'{path.name}' contains no pages")

        for page_number, page in enumerate(pdf.pages, start=1):
            try:
                title = _page_title(page)
                class_name = _extract_class_name(title)
                if not class_name:
                    class_name = f"Unknown-class-page-{page_number}"
                    warnings.append(
                        f"page {page_number}: class name not found in title; "
                        f"using '{class_name}'"
                    )

                if page_number == 1:
                    department, week_start, generated_at = _parse_metadata(page)

                vertical, horizontal = _extract_grid(page)
                day_labels = _day_labels(page, vertical[0])
                entries, page_warnings = _page_events(
                    page,
                    vertical,
                    horizontal,
                    day_labels,
                    class_name,
                    page_number,
                    source_file,
                    week_start,
                )
                warnings.extend(page_warnings)

                deduped: list[dict[str, Any]] = []
                seen_keys: set[str] = set()
                for entry in entries:
                    if entry["entry_key"] in seen_keys:
                        continue
                    seen_keys.add(entry["entry_key"])
                    deduped.append(entry)

                if not deduped:
                    warnings.append(f"page {page_number}: no course blocks found")

                classes.append(
                    {
                        "name": class_name,
                        "page": page_number,
                        "entry_count": len(deduped),
                        "entries": deduped,
                    }
                )
            except Exception as exc:
                warnings.append(f"page {page_number}: parse failed: {exc}")
                logger.warning("Timetable page %s failed: %s", page_number, exc)

    entry_count = sum(item["entry_count"] for item in classes)
    if entry_count == 0:
        raise TimetableParseError(
            "no course entries could be extracted from the PDF; " + "; ".join(warnings[:5])
        )

    return {
        "status": "parsed",
        "source_file": source_file,
        "page_count": page_count,
        "class_count": len(classes),
        "entry_count": entry_count,
        "week_start": week_start,
        "generated_at": generated_at,
        "department": department,
        "parsed_at": datetime.now(UTC).isoformat(),
        "warnings": warnings,
        "classes": classes,
    }

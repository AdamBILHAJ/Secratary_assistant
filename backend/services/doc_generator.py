"""Word (.docx) blueprint discovery, targeted editing and generation.

The module is deliberately conservative: when a blueprint is supplied, only the
elements named by the planner are touched. All other paragraphs, tables, styles
and page settings are left byte-for-byte equivalent where python-docx allows it.

Two generation modes are supported:

* ``generate_from_blueprint`` opens an existing template, applies a list of
  targeted edits (date, title, table rows, signature, ...) and returns the
  generated ``.docx`` bytes.
* ``build_standalone_document`` creates a clean document from a Gemini-designed
  ``design_spec`` (ordered blocks, palette, page setup) when no template is
  selected. Legacy ``new_doc`` payloads are adapted into the same pipeline.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph

logger = logging.getLogger(__name__)

DEFAULT_DESIGN_FONT = "Calibri"
DEFAULT_DESIGN_SIZE_PT = 11.0
DEFAULT_DESIGN_MARGIN_IN = 0.85
DEFAULT_PRIMARY_HEX = "1B365D"
DEFAULT_ACCENT_HEX = "2B6CB0"
DEFAULT_CALLOUT_HEX = "F0F4F8"
DEFAULT_TABLE_HEADER_HEX = "1B365D"
DEFAULT_TABLE_STRIPE_HEX = "F7FAFC"
MIN_FONT_SIZE_PT = 8.0
MAX_FONT_SIZE_PT = 36.0
MIN_MARGIN_IN = 0.4
MAX_MARGIN_IN = 2.0
MAX_TABLE_COLUMNS = 20
MAX_TABLE_ROWS = 200
MAX_CELL_CHARS = 2000

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
BLUEPRINT_DIR = BACKEND_DIR / "blueprints"

# Resolution order matters: uploaded templates win over the bundled examples.
_BLUEPRINT_DIRS: tuple[tuple[str, Path], ...] = (
    ("blueprints", BLUEPRINT_DIR),
    ("project_root", PROJECT_ROOT),
    ("backend", BACKEND_DIR),
    ("project_blueprints", PROJECT_ROOT / "blueprints"),
)

BLUEPRINT_REGISTRY_FILE = BLUEPRINT_DIR / "registry.json"

# Explicit allowlist of bundled templates. Prompt routing and explicit
# blueprint references can only select entries from this registry (plus
# templates explicitly registered through the upload endpoint).
_BUILTIN_BLUEPRINTS: tuple[dict[str, str], ...] = (
    {
        "id": "liste-besoins-2024",
        "filename": "Liste des Besoins 2024.docx",
        "title": "Liste des Besoins 2024",
        "description": (
            "Modèle historique de liste des besoins du département TI avec "
            "ligne de date, tableau N° / Désignation / Quantité et bloc de "
            "signature du chef de département."
        ),
        "source": "builtin",
    },
    {
        "id": "liste-besoins-sept-2024",
        "filename": "Liste des Besoins sept 2024.docx",
        "title": "Liste des Besoins septembre 2024",
        "description": (
            "Modèle de liste des besoins du département TI (version septembre "
            "2024) avec ligne de date, tableau N° / Désignation / Quantité et "
            "bloc de signature."
        ),
        "source": "builtin",
    },
)

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_DATE_PATTERN = re.compile(
    r"\b(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}-\d{2}-\d{2})\b"
)
_ISO_DATE_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_DATE_PATTERN = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")


class DocumentGenerationError(RuntimeError):
    """Base class for Word generation failures."""


class BlueprintNotFoundError(DocumentGenerationError):
    """Raised when an allowlisted blueprint file cannot be resolved."""


class BlueprintNotAllowedError(DocumentGenerationError):
    """Raised when a blueprint reference is not in the explicit allowlist."""


class InvalidBlueprintError(DocumentGenerationError):
    """Raised when a file exists but cannot be opened as a Word document."""


# ---------------------------------------------------------------------------
# Blueprint allowlist registry
# ---------------------------------------------------------------------------


def _normalize(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize(value)).strip("-")
    return slug or "blueprint"


def _coerce_registry_entry(
    raw: Any, *, default_source: str = "uploaded"
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    filename = Path(str(raw.get("filename") or "").strip()).name
    if not filename or not filename.casefold().endswith(".docx"):
        return None

    identifier = str(raw.get("id") or "").strip()
    if not identifier:
        identifier = f"custom-{_slugify(Path(filename).stem)}"

    title = str(raw.get("title") or "").strip() or Path(filename).stem
    description = str(raw.get("description") or "").strip() or (
        "Modèle Word enregistré dans l'allowlist."
    )
    return {
        "id": identifier,
        "filename": filename,
        "title": title,
        "description": description,
        "source": str(raw.get("source") or default_source),
        "uploaded_at": raw.get("uploaded_at"),
    }


def _read_custom_registry() -> list[dict[str, Any]]:
    if not BLUEPRINT_REGISTRY_FILE.is_file():
        return []
    try:
        payload = json.loads(BLUEPRINT_REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read blueprint registry: %s", exc)
        return []

    raw_entries = payload.get("blueprints") if isinstance(payload, dict) else payload
    if not isinstance(raw_entries, list):
        return []

    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        entry = _coerce_registry_entry(raw, default_source="uploaded")
        if entry is not None:
            entries.append(entry)
    return entries


def _write_custom_registry(entries: list[dict[str, Any]]) -> None:
    BLUEPRINT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = BLUEPRINT_REGISTRY_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"blueprints": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(BLUEPRINT_REGISTRY_FILE)


def _find_approved_file(filename: str) -> tuple[str, Path] | None:
    """Resolve an exact basename inside the approved directories only."""
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename.strip():
        return None
    for location, directory in _BLUEPRINT_DIRS:
        candidate = directory / safe_name
        if candidate.is_file() and candidate.suffix.casefold() == ".docx":
            return location, candidate
    return None


def _allowlist_entries() -> list[dict[str, Any]]:
    """Return resolved allowlist entries, custom uploads first."""
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_filenames: set[str] = set()

    raw_entries = _read_custom_registry() + [
        dict(entry) for entry in _BUILTIN_BLUEPRINTS
    ]
    for raw in raw_entries:
        entry = _coerce_registry_entry(raw, default_source="builtin")
        if entry is None:
            continue
        identifier = entry["id"]
        filename_key = entry["filename"].casefold()
        if identifier.casefold() in seen_ids or filename_key in seen_filenames:
            continue

        resolved = _find_approved_file(entry["filename"])
        if resolved is None:
            logger.warning(
                "Allowlisted blueprint %r is not present in approved directories",
                entry["filename"],
            )
            continue

        location, path = resolved
        entry["location"] = location
        entry["path"] = path
        seen_ids.add(identifier.casefold())
        seen_filenames.add(filename_key)
        entries.append(entry)

    return entries


def _candidate_files() -> Iterable[tuple[str, Path]]:
    """Yield ``(location, path)`` for every discoverable ``.docx`` file."""
    seen: set[Path] = set()
    for location, directory in _BLUEPRINT_DIRS:
        if not directory.is_dir():
            continue
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        paths = sorted(
            (
                path
                for path in entries
                if path.is_file()
                and path.suffix.casefold() == ".docx"
                and not path.name.startswith("~$")
            ),
            key=lambda item: item.name.casefold(),
        )
        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)
            yield location, path


def register_uploaded_blueprint(
    filename: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Explicitly add an uploaded template to the allowlist registry."""
    safe_name = Path(filename).name
    if not safe_name or not safe_name.casefold().endswith(".docx"):
        raise ValueError("A .docx filename is required.")
    if _find_approved_file(safe_name) is None:
        raise ValueError(f"Uploaded file '{safe_name}' was not found.")

    entries = _read_custom_registry()
    for entry in entries:
        if entry["filename"].casefold() == safe_name.casefold():
            entry["title"] = title or entry["title"]
            entry["description"] = description or entry["description"]
            entry["uploaded_at"] = datetime.now(UTC).isoformat()
            _write_custom_registry(entries)
            return entry

    base_identifier = f"custom-{_slugify(Path(safe_name).stem)}"
    existing_ids = {entry["id"].casefold() for entry in entries}
    existing_ids.update(
        str(entry["id"]).casefold() for entry in _BUILTIN_BLUEPRINTS
    )
    identifier = base_identifier
    suffix = 2
    while identifier.casefold() in existing_ids:
        identifier = f"{base_identifier}-{suffix}"
        suffix += 1

    entry = {
        "id": identifier,
        "filename": safe_name,
        "title": title or Path(safe_name).stem,
        "description": description
        or "Modèle Word importé et enregistré dans l'allowlist.",
        "source": "uploaded",
        "uploaded_at": datetime.now(UTC).isoformat(),
    }
    entries.append(entry)
    _write_custom_registry(entries)
    return entry


def list_blueprints() -> list[dict[str, Any]]:
    """Return clean metadata for every allowlisted ``.docx`` blueprint."""
    blueprints: list[dict[str, Any]] = []
    for entry in _allowlist_entries():
        path: Path = entry["path"]
        try:
            stat = path.stat()
            structure = summarize_blueprint(path)
        except Exception as exc:
            logger.warning("Could not summarize blueprint %s: %s", path, exc)
            continue

        blueprints.append(
            {
                "id": entry["id"],
                "name": entry["title"],
                "filename": path.name,
                "title": entry["title"],
                "description": entry["description"],
                "source": entry["source"],
                "location": entry["location"],
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=UTC
                ).isoformat(),
                "structure": structure,
            }
        )

    blueprints.sort(key=lambda item: (item["title"].casefold(), item["filename"].casefold()))
    return blueprints


def resolve_blueprint_entry(name: str | None) -> dict[str, Any] | None:
    """Resolve an explicit reference against the allowlist only."""
    if not name or not name.strip():
        return None

    raw_reference = name.strip()
    requested = Path(raw_reference).name
    requested_filename = (
        requested if requested.casefold().endswith(".docx") else f"{requested}.docx"
    )
    requested_stem = Path(requested_filename).stem.casefold()
    requested_key = requested_stem
    raw_key = raw_reference.casefold()

    entries = _allowlist_entries()

    for entry in entries:
        if entry["id"].casefold() in {raw_key, requested_key}:
            return entry
    for entry in entries:
        if entry["filename"].casefold() == requested_filename.casefold():
            return entry
    for entry in entries:
        if Path(entry["filename"]).stem.casefold() == requested_stem:
            return entry
    for entry in entries:
        if entry["title"].casefold() == raw_key:
            return entry

    partial = [
        entry
        for entry in entries
        if requested_stem in Path(entry["filename"]).stem.casefold()
        or requested_stem in entry["title"].casefold()
        or Path(entry["filename"]).stem.casefold() in requested_stem
    ]
    if partial:
        return partial[0]

    # Distinguish "known file but not allowlisted" from "no such file".
    for _, path in _candidate_files():
        if (
            path.name.casefold() == requested_filename.casefold()
            or path.stem.casefold() == requested_stem
        ):
            raise BlueprintNotAllowedError(
                f"Blueprint '{name}' exists but is not in the allowlist. "
                f"Allowed ids: {', '.join(entry['id'] for entry in entries) or 'none'}."
            )

    raise BlueprintNotFoundError(
        f"No allowlisted blueprint matches '{name}'. "
        f"Allowed ids: {', '.join(entry['id'] for entry in entries) or 'none'}."
    )


def resolve_blueprint(name: str | None) -> Path | None:
    """Resolve an allowlisted blueprint by id, filename, stem or title."""
    entry = resolve_blueprint_entry(name)
    return entry["path"] if entry else None


def auto_select_blueprint(
    subject: str, instructions: str | None = None
) -> Path | None:
    """Pick the most relevant allowlisted department-list blueprint."""
    haystack = _normalize(f"{subject} {instructions or ''}")
    if not any(
        marker in haystack
        for marker in ("liste des besoins", "liste de besoins", "besoins")
    ):
        return None

    candidates = [
        entry
        for entry in _allowlist_entries()
        if "besoin" in _normalize(f"{entry['filename']} {entry['title']}")
    ]
    if not candidates:
        return None

    selected = max(
        candidates,
        key=lambda entry: (
            entry["source"] == "uploaded",
            entry["path"].stat().st_mtime,
            entry["filename"].casefold(),
        ),
    )
    return selected["path"]


def describe_blueprint(path: Path, *, max_paragraphs: int = 200) -> dict[str, Any]:
    """Return a compact text/table description for the Gemini planner."""
    try:
        document = Document(str(path))
    except Exception as exc:
        raise InvalidBlueprintError(
            f"Blueprint '{path.name}' cannot be opened as a .docx file: {exc}"
        ) from exc

    paragraphs: list[dict[str, Any]] = []
    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text.strip()
        if not text:
            continue
        paragraphs.append(
            {
                "index": index,
                "style": paragraph.style.name if paragraph.style else None,
                "text": text[:500],
            }
        )
        if len(paragraphs) >= max_paragraphs:
            break

    tables: list[dict[str, Any]] = []
    for index, table in enumerate(document.tables):
        header = [_cell_text(cell)[:120] for cell in table.rows[0].cells] if table.rows else []
        rows: list[list[str]] = []
        for row in table.rows[1:101]:
            rows.append([_cell_text(cell)[:160] for cell in row.cells])
        tables.append(
            {
                "index": index,
                "header": header,
                "rows": rows,
                "row_count": max(len(table.rows) - 1, 0),
                "column_count": len(table.columns),
                "is_department_list": _is_department_table(table),
            }
        )

    return {
        "filename": path.name,
        "paragraph_count": len(document.paragraphs),
        "table_count": len(document.tables),
        "paragraphs": paragraphs,
        "tables": tables,
    }


def summarize_blueprint(path: Path) -> dict[str, Any]:
    """Return a compact headings/tables summary for routing and the UI."""
    try:
        document = Document(str(path))
    except Exception as exc:
        raise InvalidBlueprintError(
            f"Blueprint '{path.name}' cannot be opened as a .docx file: {exc}"
        ) from exc

    headings: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower() if paragraph.style else ""
        looks_like_heading = (
            style in {"title", "heading 1", "heading 2"}
            or "liste des besoins" in _normalize(text)
            or (text.isupper() and 3 <= len(text) <= 100)
        )
        if looks_like_heading and text not in headings:
            headings.append(text[:140])
        if len(headings) >= 8:
            break

    tables: list[dict[str, Any]] = []
    for index, table in enumerate(document.tables):
        header = (
            [_cell_text(cell)[:80] for cell in table.rows[0].cells]
            if table.rows
            else []
        )
        tables.append(
            {
                "index": index,
                "header": header,
                "row_count": max(len(table.rows) - 1, 0),
                "column_count": len(table.columns),
                "kind": (
                    "department_list"
                    if _is_department_table(table)
                    else "table"
                ),
            }
        )

    return {
        "headings": headings,
        "tables": tables,
        "paragraph_count": len(document.paragraphs),
        "table_count": len(document.tables),
    }


# ---------------------------------------------------------------------------
# Small python-docx helpers
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {}


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _normalize_plan(plan: Any) -> dict[str, Any]:
    data = _as_dict(plan)
    if not data:
        raise DocumentGenerationError("The Word plan must be a JSON object.")
    data["edits"] = [_as_dict(edit) for edit in data.get("edits") or []]
    data["sections"] = [_as_dict(section) for section in data.get("sections") or []]
    if data.get("new_doc"):
        data["new_doc"] = _as_dict(data["new_doc"])
    if data.get("design_spec"):
        data["design_spec"] = _as_dict(data["design_spec"])
    return data


def _cell_text(cell) -> str:
    return cell.text.replace("\r", "\n").strip()


def _leading_whitespace(text: str) -> str:
    match = re.match(r"^[ \t\u00a0]*", text or "")
    return match.group(0) if match else ""


def _set_paragraph_text(
    paragraph: Paragraph, text: str, *, preserve_indent: bool = False
) -> None:
    """Replace a paragraph's text while keeping its first run formatting."""
    value = str(text)
    if preserve_indent:
        prefix = _leading_whitespace(paragraph.text)
        if prefix and not value.startswith(prefix):
            value = prefix + value.lstrip()

    runs = list(paragraph.runs)
    if runs:
        runs[0].text = value
        for run in runs[1:]:
            run._element.getparent().remove(run._element)
    else:
        paragraph.add_run(value)


def _set_cell_text(cell, text: str) -> None:
    """Replace a table cell's text while keeping its paragraph/run formatting."""
    paragraphs = cell.paragraphs
    if not paragraphs:
        paragraphs = [cell.add_paragraph()]
    _set_paragraph_text(paragraphs[0], str(text))
    for extra in paragraphs[1:]:
        extra._element.getparent().remove(extra._element)


def _replace_span_in_paragraph(
    paragraph: Paragraph, start: int, end: int, replacement: str
) -> bool:
    """Replace ``paragraph.text[start:end]`` across run boundaries."""
    offset = 0
    inserted = False
    for run in paragraph.runs:
        run_text = run.text
        run_start = offset
        run_end = offset + len(run_text)
        offset = run_end

        overlap_start = max(start, run_start)
        overlap_end = min(end, run_end)
        if overlap_start >= overlap_end:
            continue

        local_start = overlap_start - run_start
        local_end = overlap_end - run_start
        if not inserted:
            run.text = run_text[:local_start] + replacement + run_text[local_end:]
            inserted = True
        else:
            run.text = run_text[:local_start] + run_text[local_end:]

    if not inserted:
        # The match may live in a hyperlink/field; fall back to a full rewrite.
        _set_paragraph_text(paragraph, paragraph.text[:start] + replacement + paragraph.text[end:])
    return True


def _format_date(value: str) -> str:
    text = value.strip()
    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match:
        return f"{iso_match.group(3)}/{iso_match.group(2)}/{iso_match.group(1)}"
    dmy_match = _DMY_DATE_PATTERN.search(text)
    if dmy_match:
        return (
            f"{int(dmy_match.group(1)):02d}/"
            f"{int(dmy_match.group(2)):02d}/"
            f"{dmy_match.group(3)}"
        )
    return text


def _last_paragraph(
    document, predicate
) -> Paragraph | None:
    for paragraph in reversed(document.paragraphs):
        if predicate(paragraph):
            return paragraph
    return None


def _paragraphs_after(document, target: Paragraph) -> list[Paragraph]:
    paragraphs = document.paragraphs
    for index, paragraph in enumerate(paragraphs):
        if paragraph._p is target._p:
            return paragraphs[index + 1 :]
    return []


def _is_date_line(paragraph: Paragraph) -> bool:
    text = paragraph.text
    if _DATE_PATTERN.search(text):
        return True
    normalized = _normalize(text)
    return "rade" in normalized and re.search(r"\ble\b", normalized) is not None


def _is_title_paragraph(paragraph: Paragraph) -> bool:
    normalized = _normalize(paragraph.text)
    if "liste des besoins" in normalized:
        return True
    style = (paragraph.style.name or "").lower() if paragraph.style else ""
    if style in {"title", "heading 1"}:
        return True
    stripped = paragraph.text.strip()
    return (
        len(stripped) >= 3
        and len(stripped) <= 90
        and stripped.isupper()
        and "rade" not in normalized
        and "departement" not in normalized
    )


def _is_department_table(table: Table) -> bool:
    if not table.rows:
        return False
    header = " ".join(_normalize(cell.text) for cell in table.rows[0].cells)
    return (
        ("designation" in header or "designations" in header)
        and ("quantite" in header or "qte" in header or "nombre" in header)
    )


def _select_table(document, table_index: Any = None) -> Table | None:
    tables = document.tables
    if not tables:
        return None

    if table_index is not None:
        try:
            index = int(table_index)
        except (TypeError, ValueError):
            index = None
        if index is not None and -len(tables) <= index < len(tables):
            return tables[index]

    for table in reversed(tables):
        if _is_department_table(table):
            return table
    return tables[-1]


def _column_indices(table: Table) -> tuple[int, int, int]:
    header = [_normalize(cell.text) for cell in table.rows[0].cells] if table.rows else []

    def find(*needles: str, default: int) -> int:
        for index, label in enumerate(header):
            if any(needle in label for needle in needles):
                return index
        return min(default, max(len(header) - 1, 0))

    number_index = find("n°", "no", "numero", "num", default=0)
    designation_index = find("designation", default=1)
    quantity_index = find("quantite", "qte", "nombre", default=2)
    return number_index, designation_index, quantity_index


def _find_item_row(
    table: Table, match: str | None, row_index: Any = None
):
    rows = list(table.rows)
    if len(rows) <= 1:
        return None

    if row_index is not None:
        try:
            index = int(row_index)
        except (TypeError, ValueError):
            index = None
        if index is not None:
            if index < 0:
                index += len(rows)
            if 0 < index < len(rows):
                return rows[index]

    if not match:
        return None

    _, designation_index, _ = _column_indices(table)
    needle = _normalize(match)

    for row in rows[1:]:
        value = _normalize(row.cells[designation_index].text)
        if value == needle:
            return row
    for row in rows[1:]:
        value = _normalize(row.cells[designation_index].text)
        if needle and (needle in value or value in needle):
            return row
    return None


def _next_item_number(table: Table) -> str:
    number_index, _, _ = _column_indices(table)
    highest = 0
    for row in table.rows[1:]:
        digits = re.search(r"\d+", row.cells[number_index].text)
        if digits:
            highest = max(highest, int(digits.group()))
    number = highest + 1
    return f"{number:02d}" if number < 100 else str(number)


def _renumber_table(table: Table) -> None:
    number_index, _, _ = _column_indices(table)
    for number, row in enumerate(table.rows[1:], start=1):
        _set_cell_text(
            row.cells[number_index],
            f"{number:02d}" if number < 100 else str(number),
        )


def _add_table_item(table: Table, designation: str, quantity: str | None) -> bool:
    if not table.rows:
        return False

    number_index, designation_index, quantity_index = _column_indices(table)
    number = _next_item_number(table)

    if len(table.rows) == 1:
        new_row = table.add_row()
    else:
        new_tr = copy.deepcopy(table.rows[-1]._tr)
        table._tbl.append(new_tr)
        new_row = table.rows[-1]

    _set_cell_text(new_row.cells[number_index], number)
    _set_cell_text(new_row.cells[designation_index], designation)
    _set_cell_text(new_row.cells[quantity_index], str(quantity or "01"))
    return True


def _update_table_item(
    table: Table,
    *,
    match: str | None,
    row_index: Any,
    designation: str | None,
    quantity: str | None,
    fallback_value: str | None,
) -> bool:
    row = _find_item_row(table, match, row_index)
    if row is None:
        return False

    _, designation_index, quantity_index = _column_indices(table)
    if designation is not None:
        _set_cell_text(row.cells[designation_index], designation)
    elif fallback_value:
        _set_cell_text(row.cells[designation_index], fallback_value)
    if quantity is not None:
        _set_cell_text(row.cells[quantity_index], str(quantity))
    return True


def _remove_table_item(table: Table, *, match: str | None, row_index: Any) -> bool:
    row = _find_item_row(table, match, row_index)
    if row is None or len(table.rows) <= 1:
        return False
    table._tbl.remove(row._tr)
    _renumber_table(table)
    return True


def _table_style_name(document) -> str | None:
    for table in document.tables:
        try:
            if table.style and table.style.name:
                return table.style.name
        except (KeyError, AttributeError):
            continue
    return None


def _add_table(
    document,
    columns: list[Any],
    rows: list[Any],
    table_style: str | None = None,
) -> Table | None:
    column_names = [str(column) for column in columns if str(column).strip()]
    if not column_names:
        return None

    table = document.add_table(rows=1, cols=len(column_names))
    style = table_style or "Table Grid"
    try:
        table.style = style
    except (KeyError, ValueError):
        try:
            table.style = "Table Grid"
        except (KeyError, ValueError):
            pass

    for cell, column in zip(table.rows[0].cells, column_names):
        _set_cell_text(cell, column)
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True

    for values in rows or []:
        if not isinstance(values, (list, tuple)):
            values = [values]
        row = table.add_row()
        for cell, value in zip(row.cells, values):
            _set_cell_text(cell, "" if value is None else str(value))

    _style_header_row(table)
    return table


def _shade_cell(cell, fill: str = "DCE6F1") -> None:
    """Apply a light background fill to a table cell (header styling)."""
    try:
        properties = cell._tc.get_or_add_tcPr()
        for existing in properties.findall(qn("w:shd")):
            properties.remove(existing)
        shading = OxmlElement("w:shd")
        shading.set(qn("w:val"), "clear")
        shading.set(qn("w:color"), "auto")
        shading.set(qn("w:fill"), fill)
        properties.append(shading)
    except Exception:  # pragma: no cover - depends on python-docx internals
        logger.debug("Could not shade table header cell", exc_info=True)


def _style_header_row(table: Table) -> None:
    if not table.rows:
        return
    for cell in table.rows[0].cells:
        _shade_cell(cell)
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True


def _sanitize_hex(value: Any, default: str = DEFAULT_PRIMARY_HEX) -> str:
    """Return a safe six-digit RGB hex string without a leading ``#``."""
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:]
    if re.fullmatch(r"[0-9a-fA-F]{3}", text):
        text = "".join(char * 2 for char in text)
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return text.upper()
    return default.upper()


def _sanitize_font_name(value: Any, default: str = DEFAULT_DESIGN_FONT) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80 or any(char in text for char in '\\/:*?"<>|'):
        return default
    return text


def _sanitize_font_size(value: Any, default: float = DEFAULT_DESIGN_SIZE_PT) -> float:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return default
    if size != size or size in (float("inf"), float("-inf")):
        return default
    return min(max(size, MIN_FONT_SIZE_PT), MAX_FONT_SIZE_PT)


def _sanitize_margin(value: Any, default: float = DEFAULT_DESIGN_MARGIN_IN) -> float:
    try:
        margin = float(value)
    except (TypeError, ValueError):
        return default
    if margin != margin or margin in (float("inf"), float("-inf")):
        return default
    return min(max(margin, MIN_MARGIN_IN), MAX_MARGIN_IN)


def _sanitize_align(value: Any, default: str = "left") -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "center": "center",
        "centre": "center",
        "left": "left",
        "right": "right",
        "justify": "justify",
        "justified": "justify",
    }
    return aliases.get(text, default)


def _alignment(value: Any, default: str = "left"):
    return {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    }[_sanitize_align(value, default)]


def _sanitize_level(value: Any, default: int = 1) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(level, 1), 6)


def _sanitize_table(
    headers: Any, rows: Any
) -> tuple[list[str], list[list[str]]]:
    """Cap table dimensions and normalize every row to the header count."""
    raw_headers = [str(value).strip() for value in (headers or []) if str(value).strip()]
    raw_rows = [row for row in (rows or []) if isinstance(row, (list, tuple))]

    if not raw_headers and raw_rows:
        column_count = min(
            max(len(row) for row in raw_rows), MAX_TABLE_COLUMNS
        )
        raw_headers = [f"Column {index}" for index in range(1, column_count + 1)]

    sanitized_headers = [
        value[:MAX_CELL_CHARS] for value in raw_headers[:MAX_TABLE_COLUMNS]
    ]
    column_count = len(sanitized_headers)
    if not column_count:
        return [], []

    sanitized_rows: list[list[str]] = []
    for row in raw_rows[:MAX_TABLE_ROWS]:
        values = [
            "" if value is None else str(value)[:MAX_CELL_CHARS]
            for value in list(row)[:column_count]
        ]
        values.extend([""] * (column_count - len(values)))
        sanitized_rows.append(values)
    return sanitized_headers, sanitized_rows


def _set_cell_borders(cell, color: str, *, size: int = 6) -> None:
    """Apply a single-line border to all four edges of a table cell."""
    try:
        properties = cell._tc.get_or_add_tcPr()
        borders = properties.find(qn("w:tcBorders"))
        if borders is None:
            borders = OxmlElement("w:tcBorders")
            properties.append(borders)
        for edge in ("top", "left", "bottom", "right"):
            tag = qn(f"w:{edge}")
            element = borders.find(tag)
            if element is None:
                element = OxmlElement(f"w:{edge}")
                borders.append(element)
            element.set(qn("w:val"), "single")
            element.set(qn("w:sz"), str(size))
            element.set(qn("w:space"), "0")
            element.set(qn("w:color"), color)
    except Exception:  # pragma: no cover - depends on python-docx internals
        logger.debug("Could not style table cell borders", exc_info=True)


def _apply_run_style(
    run,
    *,
    font_name: str,
    font_size: float,
    color: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
) -> None:
    run.font.name = font_name
    run.font.size = Pt(font_size)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        run.bold = bool(bold)
    if italic is not None:
        run.italic = bool(italic)


def _add_page_number(paragraph) -> None:
    """Append a Word ``PAGE`` field to a header/footer paragraph."""
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instruction, separate, placeholder, end):
        run._r.append(element)


def _append_section(
    document,
    section: Any,
    table_style: str | None = None,
) -> bool:
    data = _as_dict(section)
    heading = data.get("heading")
    body = data.get("body")
    columns = data.get("columns") or []
    rows = data.get("rows") or []

    if isinstance(columns, str):
        columns = [part.strip() for part in columns.split("|") if part.strip()]

    applied = False
    if heading:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(str(heading))
        run.bold = True
        run.font.size = Pt(14)
        applied = True

    if body:
        for line in str(body).splitlines():
            if line.strip():
                document.add_paragraph(line.strip())
                applied = True

    if columns and rows:
        if _add_table(document, columns, rows, table_style) is not None:
            applied = True
            # Keep a little breathing room before whatever follows the table.
            document.add_paragraph("")

    return applied


def _save_document(document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Targeted edits
# ---------------------------------------------------------------------------


def _apply_set_date(document, edit: Any) -> tuple[bool, str | None]:
    raw = str(_field(edit, "value") or "").strip()
    if not raw:
        return False, "set_date requires a value"

    date_match = _DATE_PATTERN.search(raw)
    date_value = _format_date(date_match.group(0)) if date_match else None

    target = _last_paragraph(document, _is_date_line)
    if target is None:
        text = raw if date_value is None else raw.replace(date_match.group(0), date_value)
        if document.paragraphs:
            document.paragraphs[0].insert_paragraph_before(text)
        else:
            document.add_paragraph(text)
        return True, None

    if date_value:
        existing = _DATE_PATTERN.search(target.text)
        if existing:
            _replace_span_in_paragraph(target, existing.start(), existing.end(), date_value)
        else:
            _set_paragraph_text(target, f"{target.text.rstrip()} {date_value}", preserve_indent=True)
    else:
        _set_paragraph_text(target, raw, preserve_indent=True)
    return True, None


def _apply_set_title(document, edit: Any) -> tuple[bool, str | None]:
    value = _field(edit, "value")
    if value is None or not str(value).strip():
        return False, "set_title requires a value"

    text = str(value).strip()
    target = _last_paragraph(document, _is_title_paragraph)
    if target is not None:
        _set_paragraph_text(target, text)
        return True, None

    # No title-like paragraph: place one just before the first table (or at
    # the top of the document when there is no table).
    if document.tables:
        paragraph = document.add_paragraph(text)
        document.tables[0]._tbl.addprevious(paragraph._p)
    elif document.paragraphs:
        document.paragraphs[0].insert_paragraph_before(text)
    else:
        document.add_paragraph(text)
    return True, None


def _apply_set_paragraph(document, edit: Any) -> tuple[bool, str | None]:
    match = _field(edit, "match")
    value = _field(edit, "value")
    if not match or value is None:
        return False, "set_paragraph requires match and value"

    needle = _normalize(str(match))
    target = _last_paragraph(
        document, lambda paragraph: needle in _normalize(paragraph.text)
    )
    if target is None:
        return False, f"No paragraph matching '{match}' was found"

    _set_paragraph_text(target, str(value), preserve_indent=True)
    return True, None


def _apply_set_header(document, edit: Any) -> tuple[bool, str | None]:
    columns = _field(edit, "columns") or []
    match = _field(edit, "match")
    if columns:
        return _apply_set_table_header(document, edit)
    if match:
        return _apply_set_paragraph(document, edit)
    return _apply_set_title(document, edit)


def _apply_set_table_header(document, edit: Any) -> tuple[bool, str | None]:
    table = _select_table(document, _field(edit, "table_index"))
    if table is None or not table.rows:
        return False, "No table is available for set_table_header"

    columns = _field(edit, "columns") or []
    if isinstance(columns, str):
        columns = [part.strip() for part in columns.split("|") if part.strip()]
    if not columns:
        value = _field(edit, "value")
        if value:
            columns = [part.strip() for part in re.split(r"[|,]", str(value)) if part.strip()]
    if not columns:
        return False, "set_table_header requires columns or a value"

    for cell, column in zip(table.rows[0].cells, columns):
        _set_cell_text(cell, str(column))
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True
    return True, None


def _apply_set_signature(document, edit: Any) -> tuple[bool, str | None]:
    name = _field(edit, "value") or _field(edit, "designation")
    title = _field(edit, "heading")
    department = _field(edit, "body")

    if not any([name, title, department]):
        return False, "set_signature requires value, heading or body"

    title_paragraph = _last_paragraph(
        document, lambda paragraph: "departement" in _normalize(paragraph.text)
    )

    name_paragraph = None
    department_paragraph = None
    if title_paragraph is not None:
        following = [
            paragraph
            for paragraph in _paragraphs_after(document, title_paragraph)
            if paragraph.text.strip()
        ]
        if following:
            department_paragraph = following[0]
        if len(following) > 1:
            name_paragraph = following[1]
        elif following and "technologie" not in _normalize(following[0].text):
            name_paragraph = following[0]
    else:
        non_empty = [paragraph for paragraph in document.paragraphs if paragraph.text.strip()]
        if non_empty:
            name_paragraph = non_empty[-1]
        if len(non_empty) > 1:
            title_paragraph = non_empty[-2]

    applied = False
    if title and title_paragraph is not None:
        _set_paragraph_text(title_paragraph, str(title), preserve_indent=True)
        applied = True
    if department and department_paragraph is not None:
        _set_paragraph_text(department_paragraph, str(department), preserve_indent=True)
        applied = True
    if name and name_paragraph is not None:
        _set_paragraph_text(name_paragraph, str(name), preserve_indent=True)
        applied = True
    if name and name_paragraph is None:
        document.add_paragraph(str(name))
        applied = True

    return applied, None if applied else "Could not locate the signature block"


def _apply_add_item(document, edit: Any) -> tuple[bool, str | None]:
    table = _select_table(document, _field(edit, "table_index"))
    if table is None:
        return False, "No table is available for add_item"

    designation = (
        _field(edit, "designation")
        or _field(edit, "value")
        or _field(edit, "match")
    )
    if not designation or not str(designation).strip():
        return False, "add_item requires a designation"

    quantity = _field(edit, "quantity")
    _add_table_item(table, str(designation).strip(), None if quantity is None else str(quantity))
    return True, None


def _tables_for_item_lookup(document, table_index: Any = None) -> list[Table]:
    """Return candidate tables, preferring an explicit index.

    For update/remove by designation, searching every table lets the editor
    find an item in an older historical section. The most recent matching
    table wins when the same item appears more than once.
    """
    if table_index is not None:
        table = _select_table(document, table_index)
        return [table] if table is not None else []
    return list(reversed(document.tables))


def _apply_update_item(document, edit: Any) -> tuple[bool, str | None]:
    tables = _tables_for_item_lookup(document, _field(edit, "table_index"))
    if not tables:
        return False, "No table is available for update_item"

    match = _field(edit, "match")
    row_index = _field(edit, "row_index")
    designation = _field(edit, "designation")
    quantity = _field(edit, "quantity")
    fallback_value = _field(edit, "value")

    for table in tables:
        updated = _update_table_item(
            table,
            match=match,
            row_index=row_index,
            designation=designation,
            quantity=quantity,
            fallback_value=fallback_value,
        )
        if updated:
            return True, None
    return False, "No matching table row was found for update_item"


def _apply_remove_item(document, edit: Any) -> tuple[bool, str | None]:
    tables = _tables_for_item_lookup(document, _field(edit, "table_index"))
    if not tables:
        return False, "No table is available for remove_item"

    match = _field(edit, "match") or _field(edit, "designation")
    for table in tables:
        removed = _remove_table_item(
            table,
            match=match,
            row_index=_field(edit, "row_index"),
        )
        if removed:
            return True, None
    return False, "No matching table row was found for remove_item"


def _apply_insert_section(document, edit: Any) -> tuple[bool, str | None]:
    applied = _append_section(document, edit, _table_style_name(document))
    if not applied:
        return False, "insert_section requires heading, body or a table"
    return True, None


_EDIT_HANDLERS = {
    "set_date": _apply_set_date,
    "set_title": _apply_set_title,
    "set_header": _apply_set_header,
    "set_table_header": _apply_set_table_header,
    "set_signature": _apply_set_signature,
    "set_paragraph": _apply_set_paragraph,
    "add_item": _apply_add_item,
    "update_item": _apply_update_item,
    "remove_item": _apply_remove_item,
    "insert_section": _apply_insert_section,
}


def _apply_edit(document, edit: Any) -> tuple[bool, str | None]:
    action = str(_field(edit, "action") or "").strip()
    handler = _EDIT_HANDLERS.get(action)
    if handler is None:
        return False, f"Unsupported edit action '{action}'"
    return handler(document, edit)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_from_blueprint(path: Path, plan: Any) -> dict[str, Any]:
    """Apply a plan to a copy of *path* and return the generated bytes."""
    try:
        document = Document(str(path))
    except Exception as exc:
        raise InvalidBlueprintError(
            f"Blueprint '{path.name}' cannot be opened as a .docx file: {exc}"
        ) from exc

    plan_data = _normalize_plan(plan)
    applied: list[str] = []
    warnings: list[str] = []

    for edit in plan_data["edits"]:
        action = str(_field(edit, "action") or "")
        try:
            ok, message = _apply_edit(document, edit)
        except Exception as exc:  # one bad edit must not lose the document
            logger.exception("Word edit '%s' failed", action)
            warnings.append(f"{action}: {type(exc).__name__}: {exc}")
            continue
        if ok:
            applied.append(action)
        elif message:
            warnings.append(f"{action}: {message}")

    table_style = _table_style_name(document)
    for section in plan_data["sections"]:
        try:
            if _append_section(document, section, table_style):
                applied.append("insert_section")
        except Exception as exc:
            logger.exception("Word section insertion failed")
            warnings.append(f"insert_section: {type(exc).__name__}: {exc}")

    return {
        "content": _save_document(document),
        "warnings": warnings,
        "applied_edits": applied,
        "blueprint": path.name,
    }


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


def _new_doc_has_schedule_table(tables: list[dict[str, Any]]) -> bool:
    for table in tables:
        normalized = _normalize(
            " ".join(str(column) for column in table.get("headers") or [])
        )
        if "jour" in normalized and ("horaire" in normalized or "heure" in normalized):
            return True
    return False


def _legacy_plan_to_new_doc(
    plan_data: dict[str, Any],
    subject: str,
    schedules: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Convert older section-based plans into the enriched new_doc shape."""
    sections: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []

    for item in plan_data.get("sections") or []:
        data = _as_dict(item)
        heading = str(data.get("heading") or "").strip() or None
        body = str(data.get("body") or "").strip()
        columns = data.get("columns") or []
        rows = data.get("rows") or []

        if columns and rows:
            tables.append(
                {
                    "title": heading,
                    "headers": [str(column) for column in columns],
                    "rows": [
                        ["" if value is None else str(value) for value in row]
                        for row in rows
                    ],
                }
            )
            continue

        sections.append(
            {
                "heading": heading,
                "paragraphs": [
                    line.strip() for line in body.splitlines() if line.strip()
                ],
                "bullets": [],
                "numbered_items": [],
            }
        )

    if schedules and not _new_doc_has_schedule_table(tables):
        tables.append(
            {
                "title": "Emploi du temps",
                "headers": ["Jour", "Horaire", "Matière", "Enseignant", "Salle", "Semaine"],
                "rows": _schedule_rows(schedules),
            }
        )

    signature_name = str(plan_data.get("signature_name") or "").strip()
    signature_title = str(plan_data.get("signature_title") or "").strip()
    signature_block = None
    if signature_name or signature_title:
        signature_block = {
            "title": signature_title or None,
            "role": None,
            "name": signature_name or None,
            "organization": None,
        }

    return {
        "title": str(plan_data.get("title") or subject).strip() or "Document",
        "subtitle": None,
        "date_line": None,
        "header_note": None,
        "sections": sections,
        "tables": tables,
        "signature_block": signature_block,
    }


def _design_spec_from_new_doc(
    new_doc: dict[str, Any],
    target_class: str | None,
    schedules: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Adapt the legacy ``new_doc`` shape into an ordered design_spec."""
    document = _as_dict(new_doc)
    blocks: list[dict[str, Any]] = []

    title = str(document.get("title") or "").strip()
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

    tables = [_as_dict(table) for table in document.get("tables") or []]
    for section in document.get("sections") or []:
        data = _as_dict(section)
        heading = str(data.get("heading") or "").strip()
        if heading:
            blocks.append({"kind": "heading", "text": heading, "level": 1})
        for value in data.get("paragraphs") or []:
            text = str(value).strip()
            if text:
                blocks.append({"kind": "paragraph", "text": text})
        bullets = [str(item).strip() for item in data.get("bullets") or []]
        bullets = [item for item in bullets if item]
        if bullets:
            blocks.append({"kind": "bullet_list", "items": bullets})
        numbered = [str(item).strip() for item in data.get("numbered_items") or []]
        numbered = [item for item in numbered if item]
        if numbered:
            blocks.append({"kind": "numbered_list", "items": numbered})

    if schedules and not _new_doc_has_schedule_table(tables):
        tables.append(
            {
                "title": "Emploi du temps",
                "headers": ["Jour", "Horaire", "Matière", "Enseignant", "Salle", "Semaine"],
                "rows": _schedule_rows(schedules),
            }
        )

    for table in tables:
        blocks.append(
            {
                "kind": "table",
                "caption": str(table.get("title") or "").strip() or None,
                "headers": table.get("headers") or [],
                "rows": table.get("rows") or [],
            }
        )

    signature = _as_dict(document.get("signature_block"))
    if signature:
        role = str(signature.get("title") or signature.get("role") or "").strip()
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
        "font_name": DEFAULT_DESIGN_FONT,
        "base_font_size_pt": DEFAULT_DESIGN_SIZE_PT,
        "margin_inches": DEFAULT_DESIGN_MARGIN_IN,
        "orientation": "portrait",
        "primary_hex": f"#{DEFAULT_PRIMARY_HEX}",
        "accent_hex": f"#{DEFAULT_ACCENT_HEX}",
        "callout_bg_hex": f"#{DEFAULT_CALLOUT_HEX}",
        "table_header_hex": f"#{DEFAULT_TABLE_HEADER_HEX}",
        "table_stripe_hex": f"#{DEFAULT_TABLE_STRIPE_HEX}",
        "header_text": None,
        "footer_text": None,
        "include_page_numbers": False,
        "blocks": blocks,
    }


def _design_has_schedule_table(blocks: list[dict[str, Any]]) -> bool:
    for block in blocks:
        if str(block.get("kind") or "") != "table":
            continue
        headers = [str(value) for value in block.get("headers") or []]
        normalized = _normalize(" ".join(headers))
        if "jour" in normalized and ("horaire" in normalized or "heure" in normalized):
            return True
    return False


def _design_context(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "font": _sanitize_font_name(spec.get("font_name")),
        "size": _sanitize_font_size(spec.get("base_font_size_pt")),
        "primary": _sanitize_hex(spec.get("primary_hex"), DEFAULT_PRIMARY_HEX),
        "accent": _sanitize_hex(spec.get("accent_hex"), DEFAULT_ACCENT_HEX),
        "callout": _sanitize_hex(spec.get("callout_bg_hex"), DEFAULT_CALLOUT_HEX),
        "table_header": _sanitize_hex(
            spec.get("table_header_hex"), DEFAULT_TABLE_HEADER_HEX
        ),
        "stripe": _sanitize_hex(spec.get("table_stripe_hex"), DEFAULT_TABLE_STRIPE_HEX),
    }


def _configure_design_page(
    document,
    spec: dict[str, Any],
    context: dict[str, Any],
    applied: list[str],
) -> None:
    section = document.sections[0]
    orientation = str(spec.get("orientation") or "portrait").strip().lower()
    width, height = section.page_width, section.page_height
    if orientation == "landscape":
        section.orientation = WD_ORIENT.LANDSCAPE
        if width < height:
            section.page_width, section.page_height = height, width
        applied.append("set_landscape")
    else:
        section.orientation = WD_ORIENT.PORTRAIT
        if width > height:
            section.page_width, section.page_height = height, width
        applied.append("set_portrait")

    margin = Inches(_sanitize_margin(spec.get("margin_inches")))
    section.top_margin = margin
    section.bottom_margin = margin
    section.left_margin = margin
    section.right_margin = margin
    applied.append("set_margins")

    normal = document.styles["Normal"]
    normal.font.name = context["font"]
    normal.font.size = Pt(context["size"])
    applied.append("set_base_font")

    small_size = min(context["size"], 9.0)
    header_text = str(spec.get("header_text") or "").strip()
    if header_text:
        paragraph = section.header.paragraphs[0]
        paragraph.text = header_text
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for run in paragraph.runs:
            _apply_run_style(
                run,
                font_name=context["font"],
                font_size=small_size,
                color=context["accent"],
            )
        applied.append("set_header")

    footer_text = str(spec.get("footer_text") or "").strip()
    include_page_numbers = bool(spec.get("include_page_numbers"))
    if footer_text or include_page_numbers:
        paragraph = section.footer.paragraphs[0]
        paragraph.text = ""
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if footer_text:
            run = paragraph.add_run(footer_text)
            _apply_run_style(
                run,
                font_name=context["font"],
                font_size=small_size,
                color=context["accent"],
            )
        if include_page_numbers:
            prefix = "  " if footer_text else ""
            run = paragraph.add_run(f"{prefix}Page ")
            _apply_run_style(
                run,
                font_name=context["font"],
                font_size=small_size,
                color=context["accent"],
            )
            _add_page_number(paragraph)
        applied.append("set_footer")


def _add_design_heading(
    document, text: str, level: Any, context: dict[str, Any]
) -> None:
    safe_level = _sanitize_level(level)
    try:
        paragraph = document.add_paragraph(text, style=f"Heading {safe_level}")
    except (KeyError, ValueError):
        paragraph = document.add_paragraph()
        run = paragraph.add_run(text)
        _apply_run_style(
            run,
            font_name=context["font"],
            font_size=max(context["size"] + 2, 12.0),
            color=context["primary"],
            bold=True,
        )
        return
    for run in paragraph.runs:
        _apply_run_style(
            run,
            font_name=context["font"],
            font_size=max(context["size"] + 2, 12.0),
            color=context["primary"],
            bold=True,
        )


def _add_design_paragraph(
    document, block: dict[str, Any], context: dict[str, Any]
) -> None:
    text = str(block.get("text") or "").strip()
    if not text:
        return
    paragraph = document.add_paragraph()
    paragraph.alignment = _alignment(block.get("align"))
    run = paragraph.add_run(text)
    _apply_run_style(
        run,
        font_name=context["font"],
        font_size=context["size"],
        bold=block.get("bold"),
        italic=block.get("italic"),
    )


def _add_design_list(
    document,
    items: list[Any] | None,
    context: dict[str, Any],
    *,
    numbered: bool,
) -> int:
    style = "List Number" if numbered else "List Bullet"
    added = 0
    for index, item in enumerate(items or [], start=1):
        text = str(item).strip()
        if not text:
            continue
        try:
            paragraph = document.add_paragraph(text, style=style)
        except (KeyError, ValueError):
            prefix = f"{index}. " if numbered else "• "
            paragraph = document.add_paragraph(prefix + text)
        for run in paragraph.runs:
            _apply_run_style(run, font_name=context["font"], font_size=context["size"])
        added += 1
    return added


def _add_design_callout(
    document, block: dict[str, Any], context: dict[str, Any]
) -> bool:
    title = str(block.get("title") or "").strip()
    text = str(block.get("text") or "").strip()
    if not title and not text:
        return False

    table = document.add_table(rows=1, cols=1)
    try:
        table.style = "Table Grid"
    except (KeyError, ValueError):
        pass
    cell = table.rows[0].cells[0]
    if title:
        heading = cell.paragraphs[0]
        run = heading.add_run(title)
        _apply_run_style(
            run,
            font_name=context["font"],
            font_size=context["size"],
            color=context["primary"],
            bold=True,
        )
        if text:
            body = cell.add_paragraph()
            run = body.add_run(text)
            _apply_run_style(
                run,
                font_name=context["font"],
                font_size=context["size"],
                italic=True,
            )
    else:
        run = cell.paragraphs[0].add_run(text)
        _apply_run_style(
            run,
            font_name=context["font"],
            font_size=context["size"],
            italic=True,
        )

    fill = _sanitize_hex(block.get("fill_hex"), context["callout"])
    border = _sanitize_hex(block.get("border_hex"), context["accent"])
    _shade_cell(cell, fill)
    _set_cell_borders(cell, border, size=8)
    document.add_paragraph("")
    return True


def _add_design_table(
    document, block: dict[str, Any], context: dict[str, Any]
) -> bool:
    caption = str(block.get("caption") or "").strip()
    if caption:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(caption)
        _apply_run_style(
            run,
            font_name=context["font"],
            font_size=context["size"],
            color=context["primary"],
            bold=True,
        )

    headers, rows = _sanitize_table(block.get("headers"), block.get("rows"))
    if not headers:
        return False
    table = _add_table(document, headers, rows, "Table Grid")
    if table is None:
        return False

    border = _sanitize_hex(block.get("border_hex"), context["primary"])
    for row_index, row in enumerate(table.rows):
        for cell in row.cells:
            _set_cell_borders(cell, border)
            if row_index == 0:
                _shade_cell(cell, context["table_header"])
            elif row_index % 2 == 0:
                _shade_cell(cell, context["stripe"])
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    _apply_run_style(
                        run,
                        font_name=context["font"],
                        font_size=context["size"],
                        color="FFFFFF" if row_index == 0 else None,
                        bold=row_index == 0,
                    )
    document.add_paragraph("")
    return True


def _add_design_signature(
    document, block: dict[str, Any], context: dict[str, Any]
) -> bool:
    role = str(block.get("signatory_role") or "").strip()
    name = str(block.get("signatory_name") or "").strip()
    if not role and not name:
        return False

    document.add_paragraph("")
    for value, is_role in ((role, True), (name, False)):
        if not value:
            continue
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run = paragraph.add_run(value)
        _apply_run_style(
            run,
            font_name=context["font"],
            font_size=context["size"],
            color=context["primary"] if is_role else None,
            bold=is_role,
        )
    return True


def _render_design_spec(
    document,
    spec: dict[str, Any],
    applied: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    context = _design_context(spec)
    _configure_design_page(document, spec, context, applied)

    for raw_block in spec.get("blocks") or []:
        block = _as_dict(raw_block)
        kind = str(block.get("kind") or "").strip()
        try:
            if kind == "title_block":
                kicker = str(block.get("kicker") or "").strip()
                if kicker:
                    paragraph = document.add_paragraph()
                    paragraph.alignment = _alignment(
                        block.get("align"), "center"
                    )
                    run = paragraph.add_run(kicker)
                    _apply_run_style(
                        run,
                        font_name=context["font"],
                        font_size=max(context["size"] - 1, MIN_FONT_SIZE_PT),
                        color=context["accent"],
                        bold=True,
                    )

                title = str(block.get("title") or "").strip()
                if title:
                    paragraph = document.add_paragraph()
                    paragraph.alignment = _alignment(
                        block.get("align"), "center"
                    )
                    run = paragraph.add_run(title)
                    _apply_run_style(
                        run,
                        font_name=context["font"],
                        font_size=min(max(context["size"] + 7, 18.0), MAX_FONT_SIZE_PT),
                        color=context["primary"],
                        bold=True,
                    )

                subtitle = str(block.get("subtitle") or "").strip()
                if subtitle:
                    paragraph = document.add_paragraph()
                    paragraph.alignment = _alignment(
                        block.get("align"), "center"
                    )
                    run = paragraph.add_run(subtitle)
                    _apply_run_style(
                        run,
                        font_name=context["font"],
                        font_size=min(max(context["size"] + 1, 12.0), MAX_FONT_SIZE_PT),
                        color=context["accent"],
                        italic=True,
                    )
                applied.append("insert_title_block")
            elif kind == "heading":
                text = str(block.get("text") or block.get("title") or "").strip()
                if text:
                    _add_design_heading(document, text, block.get("level"), context)
                    applied.append("insert_heading")
            elif kind == "paragraph":
                _add_design_paragraph(document, block, context)
                applied.append("insert_paragraph")
            elif kind == "callout":
                if _add_design_callout(document, block, context):
                    applied.append("insert_callout")
            elif kind == "bullet_list":
                if _add_design_list(
                    document, block.get("items"), context, numbered=False
                ):
                    applied.append("insert_bullets")
            elif kind == "numbered_list":
                if _add_design_list(
                    document, block.get("items"), context, numbered=True
                ):
                    applied.append("insert_numbered_items")
            elif kind == "table":
                if _add_design_table(document, block, context):
                    applied.append("insert_table")
            elif kind == "page_break":
                document.add_page_break()
                applied.append("page_break")
            elif kind == "signature_block":
                if _add_design_signature(document, block, context):
                    applied.append("set_signature")
            else:
                warnings.append(f"unknown_design_block: {kind}")
                logger.warning("Unknown design_spec block kind: %s", kind)
        except Exception as exc:
            logger.exception("Failed to render design_spec block %s", kind)
            warnings.append(f"design_block:{kind}: {type(exc).__name__}: {exc}")
    return context


def build_standalone_document(
    plan: Any,
    subject: str,
    target_class: str | None = None,
    schedules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a clean Word document from ``design_spec`` (or legacy ``new_doc``)."""
    plan_data = _normalize_plan(plan)
    new_doc = _as_dict(plan_data.get("new_doc")) if plan_data.get("new_doc") else None
    if not new_doc:
        new_doc = _legacy_plan_to_new_doc(plan_data, subject, schedules)

    provided_spec = (
        _as_dict(plan_data.get("design_spec")) if plan_data.get("design_spec") else None
    )
    provided_blocks = (
        [_as_dict(block) for block in provided_spec.get("blocks") or []]
        if provided_spec
        else []
    )

    if provided_spec and provided_blocks:
        spec = dict(provided_spec)
        spec["blocks"] = provided_blocks
    else:
        generated = _design_spec_from_new_doc(new_doc, target_class, schedules)
        if provided_spec:
            # Keep Gemini's styling choices but supply the missing blocks.
            spec = {
                **generated,
                **{
                    key: value
                    for key, value in provided_spec.items()
                    if key != "blocks"
                },
            }
            spec["blocks"] = generated["blocks"]
        else:
            spec = generated

    blocks = [_as_dict(block) for block in spec.get("blocks") or []]
    if schedules and not _design_has_schedule_table(blocks):
        blocks.append(
            {
                "kind": "table",
                "caption": "Emploi du temps",
                "headers": ["Jour", "Horaire", "Matière", "Enseignant", "Salle", "Semaine"],
                "rows": _schedule_rows(schedules),
            }
        )
    spec["blocks"] = blocks

    document = Document()
    applied: list[str] = []
    warnings: list[str] = []
    _render_design_spec(document, spec, applied, warnings)

    title = str(new_doc.get("title") or "").strip() or str(subject or "Document").strip()
    for block in blocks:
        kind = str(block.get("kind") or "").strip()
        if kind == "title_block":
            value = str(block.get("title") or "").strip()
            if value:
                title = value
                break
        elif kind == "heading":
            value = str(block.get("text") or block.get("title") or "").strip()
            if value:
                title = value
                break
    document.core_properties.title = title

    return {
        "content": _save_document(document),
        "warnings": warnings,
        "applied_edits": applied,
        "blueprint": None,
    }


def execute_word_decision(
    decision: Any,
    *,
    prompt: str,
    target_class: str | None = None,
    schedules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute a routed Word decision and return generated document bytes.

    ``use_blueprint`` keeps the selected template with only an optional date
    edit. ``modify_blueprint`` applies the planner's targeted edits on a copy.
    ``create_new`` builds a clean document from ``design_spec`` (falling back
    to the legacy ``new_doc`` structure when no design is supplied).
    """
    data = _as_dict(decision)
    action = str(data.get("action") or "create_new").strip()
    blueprint_id = data.get("blueprint_id")
    new_doc = _as_dict(data.get("new_doc")) if data.get("new_doc") else None
    design_spec = (
        _as_dict(data.get("design_spec")) if data.get("design_spec") else None
    )
    design_title = ""
    for raw_block in (design_spec or {}).get("blocks") or []:
        block = _as_dict(raw_block)
        if str(block.get("kind") or "") == "title_block":
            design_title = str(block.get("title") or "").strip()
            if design_title:
                break
    title = str(
        (new_doc or {}).get("title") or design_title or data.get("title") or ""
    ).strip()

    plan = {
        "title": title or prompt.strip().splitlines()[0][:140],
        "summary": data.get("summary") or "",
        "signature_title": data.get("signature_title"),
        "signature_name": data.get("signature_name"),
        "edits": [_as_dict(edit) for edit in data.get("edits") or []],
        "sections": [_as_dict(section) for section in data.get("sections") or []],
        "new_doc": new_doc,
        "design_spec": design_spec,
    }

    if action in {"use_blueprint", "modify_blueprint"}:
        entry = resolve_blueprint_entry(str(blueprint_id) if blueprint_id else None)
        if entry is None:
            raise BlueprintNotAllowedError(
                "The routed blueprint is not in the allowlist."
            )
        if action == "use_blueprint":
            plan["edits"] = [
                edit
                for edit in plan["edits"]
                if str(_field(edit, "action") or "") == "set_date"
            ]
            plan["sections"] = []

        result = generate_from_blueprint(entry["path"], plan)
        result.update(
            {
                "action": action,
                "blueprint_id": entry["id"],
                "blueprint_title": entry["title"],
            }
        )
        return result

    plan["edits"] = []
    result = build_standalone_document(
        plan,
        title or prompt,
        target_class=target_class,
        schedules=schedules,
    )
    result.update(
        {
            "action": "create_new",
            "blueprint_id": None,
            "blueprint_title": None,
        }
    )
    return result

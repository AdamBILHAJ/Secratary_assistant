"""Regression tests for prompt-driven Word routing and standalone rendering.

Run from the repository root with::

    python -m unittest discover -s backend/tests -v

The tests use FastAPI's ``TestClient`` and a stubbed Gemini client, so no
network access or API key is required.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import services.gemini_service as gemini_service  # noqa: E402
from main import app  # noqa: E402
from services.doc_generator import execute_word_decision  # noqa: E402


def _docx_text(content: bytes) -> str:
    document = Document(BytesIO(content))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def _cell_fill(cell) -> str | None:
    properties = cell._tc.tcPr
    shading = properties.find(qn("w:shd")) if properties is not None else None
    return shading.get(qn("w:fill")) if shading is not None else None


def _cell_border(cell, edge: str = "top") -> str | None:
    properties = cell._tc.tcPr
    borders = properties.find(qn("w:tcBorders")) if properties is not None else None
    if borders is None:
        return None
    element = borders.find(qn(f"w:{edge}"))
    return element.get(qn("w:color")) if element is not None else None


class WordRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self._old_api_key = os.environ.pop("GEMINI_API_KEY", None)
        self.blueprints = self.client.get("/api/documents/blueprints").json()[
            "blueprints"
        ]
        self.assertTrue(self.blueprints, "expected allowlisted blueprints")

    def tearDown(self) -> None:
        if self._old_api_key is not None:
            os.environ["GEMINI_API_KEY"] = self._old_api_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)

    def _route_with_decision(
        self,
        decision: gemini_service.WordRouteDecision,
        prompt: str,
        *,
        explicit_blueprint_id: str | None = None,
    ) -> dict:
        """Run the router with a canned Gemini response."""

        class Response:
            parsed = decision

        class Models:
            async def generate_content(self, **kwargs):  # noqa: ANN003
                return Response()

        class FakeClient:
            def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.aio = SimpleNamespace(models=Models())

        original_client = gemini_service.genai.Client
        gemini_service.genai.Client = FakeClient
        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            return asyncio.run(
                gemini_service.route_word_document(
                    prompt,
                    catalog=self.blueprints,
                    explicit_blueprint_id=explicit_blueprint_id,
                )
            )
        finally:
            gemini_service.genai.Client = original_client
            os.environ.pop("GEMINI_API_KEY", None)

    def test_01_auto_from_scratch_creates_new_document(self) -> None:
        response = self.client.post(
            "/api/documents/generate",
            json={
                "prompt": (
                    "Create a brand new report from scratch about quarterly "
                    "planning"
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Doc-Action"], "create_new")
        self.assertEqual(response.headers["X-AI-Source"], "fallback")
        text = _docx_text(response.content)
        self.assertNotIn("Radès", text)

    def test_02_auto_unrelated_topic_creates_new_document_without_forced_date(
        self,
    ) -> None:
        response = self.client.post(
            "/api/documents/generate",
            json={"prompt": "Generate a Word doc about climate data"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Doc-Action"], "create_new")
        text = _docx_text(response.content)
        self.assertNotIn("Radès", text)
        self.assertNotIn("Le Chef du Département", text)

        empty_override = self.client.post(
            "/api/documents/generate",
            json={
                "prompt": "Generate a Word doc about climate data",
                "blueprint_id": "",
            },
        )
        self.assertEqual(empty_override.headers["X-Doc-Action"], "create_new")

    def test_03_invalid_blueprint_id_is_hard_gated(self) -> None:
        decision = gemini_service.WordRouteDecision(
            action="use_blueprint",
            blueprint_id="not-allowlisted",
            title="Broken route",
            blueprint_confidence=0.99,
        )
        routed = self._route_with_decision(decision, "Use my template please")
        result = routed["decision"]
        self.assertEqual(result["action"], "create_new")
        self.assertIsNone(result["blueprint_id"])
        self.assertTrue(result["new_doc"])

    def test_04_low_confidence_blueprint_is_hard_gated(self) -> None:
        decision = gemini_service.WordRouteDecision(
            action="modify_blueprint",
            blueprint_id=self.blueprints[0]["id"],
            title="Maybe a blueprint",
            blueprint_confidence=0.4,
        )
        routed = self._route_with_decision(decision, "Liste des besoins maybe")
        result = routed["decision"]
        self.assertEqual(result["action"], "create_new")
        self.assertIsNone(result["blueprint_id"])
        self.assertTrue(result["new_doc"])

    def test_05_explicit_scratch_prompt_overrides_blueprint_route(self) -> None:
        decision = gemini_service.WordRouteDecision(
            action="modify_blueprint",
            blueprint_id=self.blueprints[0]["id"],
            title="Bad route",
            blueprint_confidence=0.95,
        )
        routed = self._route_with_decision(
            decision, "Create a brand new report from scratch about audits"
        )
        result = routed["decision"]
        self.assertEqual(result["action"], "create_new")
        self.assertIsNone(result["blueprint_id"])
        self.assertTrue(result["new_doc"])

    def test_06_auto_needs_list_still_modifies_blueprint(self) -> None:
        response = self.client.post(
            "/api/documents/generate",
            json={
                "prompt": (
                    "Add 10 HDMI cables and remove scotch from the Liste des "
                    "Besoins, date 22/11/2026"
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Doc-Action"], "modify_blueprint")
        self.assertIn("liste", response.headers["X-Blueprint-Used"].lower())
        self.assertIn("HDMI", _docx_text(response.content))

    def test_07_explicit_blueprint_override_is_honored(self) -> None:
        blueprint_id = self.blueprints[0]["id"]
        response = self.client.post(
            "/api/documents/generate",
            json={
                "prompt": "Generate a Word doc about climate data",
                "blueprint_id": blueprint_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            response.headers["X-Doc-Action"],
            {"use_blueprint", "modify_blueprint"},
        )
        self.assertEqual(response.headers["X-Blueprint-Used"], blueprint_id)

        # An explicit override also wins over a Gemini create_new decision.
        decision = gemini_service.WordRouteDecision(
            action="create_new", title="New document"
        )
        routed = self._route_with_decision(
            decision,
            "Generate a Word doc about climate data",
            explicit_blueprint_id=blueprint_id,
        )
        self.assertEqual(routed["decision"]["blueprint_id"], blueprint_id)
        self.assertNotEqual(routed["decision"]["action"], "create_new")

    def test_08_enriched_new_doc_renders_all_blocks(self) -> None:
        new_doc = {
            "title": "Rapport annuel",
            "subtitle": "Version préliminaire",
            "date_line": "Paris, le 22/11/2026",
            "header_note": "Note introductive",
            "sections": [
                {
                    "heading": "Synthèse",
                    "paragraphs": ["Premier paragraphe."],
                    "bullets": ["Point A", "Point B"],
                    "numbered_items": ["Étape 1", "Étape 2"],
                }
            ],
            "tables": [
                {
                    "title": "Tableau de suivi",
                    "headers": ["Tâche", "Statut"],
                    "rows": [["A", "Fait"]],
                }
            ],
            "signature_block": {
                "title": "Le Directeur",
                "name": "Jean Dupont",
            },
        }
        decision = {
            "action": "create_new",
            "blueprint_id": None,
            "title": new_doc["title"],
            "new_doc": new_doc,
        }
        result = execute_word_decision(decision, prompt="Create a new report")
        document = Document(BytesIO(result["content"]))
        text = _docx_text(result["content"])
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        styles = [paragraph.style.name for paragraph in document.paragraphs]

        self.assertIn("Rapport annuel", text)
        self.assertIn("Version préliminaire", text)
        self.assertIn("Paris, le 22/11/2026", text)
        self.assertIn("Note introductive", text)
        self.assertIn("Synthèse", text)
        self.assertIn("Point A", text)
        self.assertIn("Étape 1", text)
        self.assertIn("Tableau de suivi", text)
        self.assertIn("Jean Dupont", text)
        self.assertIn("Heading 1", styles)
        self.assertIn("List Bullet", styles)
        self.assertIn("List Number", styles)
        # The legacy header_note becomes a callout table, followed by the data table.
        self.assertEqual(len(document.tables), 2)
        self.assertEqual(
            [cell.text for cell in document.tables[1].rows[0].cells],
            ["Tâche", "Statut"],
        )

    def test_09_design_spec_renders_custom_style_and_blocks(self) -> None:
        spec = {
            "font_name": "Aptos",
            "base_font_size_pt": 12.5,
            "margin_inches": 0.6,
            "orientation": "landscape",
            "primary_hex": "#123456",
            "accent_hex": "#ABCDEF",
            "callout_bg_hex": "#FFF0D0",
            "table_header_hex": "#123456",
            "table_stripe_hex": "#EEEEEE",
            "header_text": "Running header",
            "footer_text": "Confidential",
            "include_page_numbers": True,
            "blocks": [
                {
                    "kind": "title_block",
                    "kicker": "ANNUAL",
                    "title": "Rapport annuel",
                    "subtitle": "Version 2026",
                    "align": "center",
                },
                {"kind": "heading", "text": "Synthèse", "level": 2},
                {
                    "kind": "callout",
                    "title": "Important",
                    "text": "À lire en priorité",
                    "fill_hex": "#FFF0D0",
                    "border_hex": "#ABCDEF",
                },
                {
                    "kind": "paragraph",
                    "text": "Paragraphe personnalisé",
                    "bold": True,
                    "italic": True,
                    "align": "sideways",
                },
                {"kind": "bullet_list", "items": ["Point A", "Point B"]},
                {"kind": "numbered_list", "items": ["Étape 1", "Étape 2"]},
                {
                    "kind": "table",
                    "caption": "Suivi",
                    "headers": ["Tâche", "Statut"],
                    "rows": [["A", "Fait"], ["B", "En cours"], ["C", "À faire"]],
                },
                {"kind": "page_break"},
                {
                    "kind": "signature_block",
                    "signatory_role": "Le Directeur",
                    "signatory_name": "Jean Dupont",
                },
            ],
        }
        result = execute_word_decision(
            {"action": "create_new", "title": "Rapport annuel", "design_spec": spec},
            prompt="Create a new report",
        )
        document = Document(BytesIO(result["content"]))
        section = document.sections[0]

        self.assertEqual(document.styles["Normal"].font.name, "Aptos")
        self.assertEqual(document.styles["Normal"].font.size.pt, 12.5)
        self.assertEqual(section.orientation, WD_ORIENT.LANDSCAPE)
        self.assertGreater(section.page_width, section.page_height)
        self.assertAlmostEqual(section.top_margin.inches, 0.6)
        self.assertEqual(
            [paragraph.text for paragraph in section.header.paragraphs],
            ["Running header"],
        )
        footer = section.footer.paragraphs[0]
        self.assertIn("Confidential", footer.text)
        self.assertIn("PAGE", footer._p.xml)

        text = _docx_text(result["content"])
        for value in (
            "ANNUAL",
            "Rapport annuel",
            "Version 2026",
            "Synthèse",
            "À lire en priorité",
            "Paragraphe personnalisé",
            "Point A",
            "Étape 1",
            "Suivi",
            "Jean Dupont",
        ):
            self.assertIn(value, text)

        title_paragraph = next(
            paragraph for paragraph in document.paragraphs if paragraph.text == "Rapport annuel"
        )
        self.assertEqual(str(title_paragraph.runs[0].font.color.rgb), "123456")
        body_paragraph = next(
            paragraph
            for paragraph in document.paragraphs
            if paragraph.text == "Paragraphe personnalisé"
        )
        self.assertEqual(body_paragraph.alignment, WD_ALIGN_PARAGRAPH.LEFT)
        self.assertTrue(body_paragraph.runs[0].bold)
        self.assertTrue(body_paragraph.runs[0].italic)

        self.assertEqual(len(document.tables), 2)
        callout_cell = document.tables[0].rows[0].cells[0]
        self.assertEqual(_cell_fill(callout_cell), "FFF0D0")
        self.assertEqual(_cell_border(callout_cell), "ABCDEF")

        data_table = document.tables[1]
        self.assertEqual(
            [cell.text for cell in data_table.rows[0].cells], ["Tâche", "Statut"]
        )
        self.assertEqual(_cell_fill(data_table.rows[0].cells[0]), "123456")
        self.assertTrue(
            any(
                _cell_fill(cell) == "EEEEEE"
                for row in data_table.rows[1:]
                for cell in row.cells
            )
        )
        self.assertIn("page_break", result["applied_edits"])

    def test_10_design_spec_portrait_margins(self) -> None:
        spec = {
            "orientation": "portrait",
            "margin_inches": 1.4,
            "blocks": [{"kind": "paragraph", "text": "Portrait"}],
        }
        result = execute_word_decision(
            {"action": "create_new", "design_spec": spec}, prompt="Create a document"
        )
        section = Document(BytesIO(result["content"])).sections[0]
        self.assertEqual(section.orientation, WD_ORIENT.PORTRAIT)
        self.assertGreater(section.page_height, section.page_width)
        self.assertAlmostEqual(section.left_margin.inches, 1.4)
        self.assertAlmostEqual(section.right_margin.inches, 1.4)

    def test_11_invalid_design_values_are_sanitized(self) -> None:
        spec = {
            "font_name": "",
            "base_font_size_pt": 99,
            "margin_inches": 99,
            "primary_hex": "not-a-color",
            "table_header_hex": "bad!",
            "blocks": [
                {"kind": "heading", "text": "Titre sûr", "level": 99},
                {
                    "kind": "paragraph",
                    "text": "Alignement sûr",
                    "align": "sideways",
                },
                {
                    "kind": "table",
                    "headers": [f"Col {index}" for index in range(25)],
                    "rows": [
                        [f"cell {index}" for index in range(25)]
                        for _ in range(205)
                    ],
                },
            ],
        }
        result = execute_word_decision(
            {"action": "create_new", "design_spec": spec}, prompt="Create a document"
        )
        document = Document(BytesIO(result["content"]))

        self.assertEqual(document.styles["Normal"].font.name, "Calibri")
        self.assertEqual(document.styles["Normal"].font.size.pt, 36.0)
        self.assertAlmostEqual(document.sections[0].top_margin.inches, 2.0)

        heading = next(
            paragraph
            for paragraph in document.paragraphs
            if paragraph.text == "Titre sûr"
        )
        self.assertEqual(heading.style.name, "Heading 6")
        body = next(
            paragraph
            for paragraph in document.paragraphs
            if paragraph.text == "Alignement sûr"
        )
        self.assertEqual(body.alignment, WD_ALIGN_PARAGRAPH.LEFT)

        self.assertEqual(len(document.tables), 1)
        table = document.tables[0]
        self.assertEqual(len(table.columns), 20)
        self.assertEqual(len(table.rows), 201)  # header + 200 capped rows
        self.assertEqual(_cell_fill(table.rows[0].cells[0]), "1B365D")
        self.assertEqual(result["warnings"], [])

    def test_12_fallback_builds_design_spec(self) -> None:
        routed = asyncio.run(
            gemini_service.route_word_document(
                "Generate a Word doc about climate data", catalog=self.blueprints
            )
        )
        decision = routed["decision"]
        self.assertEqual(routed["source"], "fallback")
        self.assertEqual(decision["action"], "create_new")
        self.assertTrue(decision["design_spec"]["blocks"])

        result = execute_word_decision(
            decision, prompt="Generate a Word doc about climate data"
        )
        text = _docx_text(result["content"])
        self.assertIn("climate data", text.lower())
        self.assertNotIn("Radès", text)


if __name__ == "__main__":
    unittest.main()

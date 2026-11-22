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
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        styles = [paragraph.style.name for paragraph in document.paragraphs]

        self.assertIn("Rapport annuel", paragraphs)
        self.assertIn("Version préliminaire", paragraphs)
        self.assertIn("Paris, le 22/11/2026", paragraphs)
        self.assertIn("Note introductive", paragraphs)
        self.assertIn("Synthèse", paragraphs)
        self.assertIn("Point A", paragraphs)
        self.assertIn("Étape 1", paragraphs)
        self.assertIn("Tableau de suivi", paragraphs)
        self.assertIn("Jean Dupont", paragraphs)
        self.assertIn("Heading 1", styles)
        self.assertIn("List Bullet", styles)
        self.assertIn("List Number", styles)
        self.assertEqual(len(document.tables), 1)
        self.assertEqual(
            [cell.text for cell in document.tables[0].rows[0].cells],
            ["Tâche", "Statut"],
        )


if __name__ == "__main__":
    unittest.main()

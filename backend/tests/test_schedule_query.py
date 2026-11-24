"""Tests for unified natural-language room and schedule Q&A.

Run from the repository root with::

    python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import routes.schedules as schedules_route  # noqa: E402
import services.gemini_service as gemini_service  # noqa: E402
import services.schedule_query as schedule_query  # noqa: E402
from main import app  # noqa: E402
from services.gemini_service import ScheduleQueryDecision  # noqa: E402

SAMPLE_SESSION = {
    "entry_key": "c9-monday",
    "class_name": "L1-TI-1",
    "day": "lundi",
    "time_slot": "10:15-11:45",
    "start_time": "10:15",
    "end_time": "11:45",
    "slots": ["10:15-11:45"],
    "subject": "Réseaux",
    "teacher": "Dupont",
    "room": "C9",
    "week": "Q2",
    "group": "G2",
}


def _with_gemini_decision(decision: ScheduleQueryDecision, coroutine_factory):
    """Run a coroutine with a stubbed Gemini client returning ``decision``."""

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
        return asyncio.run(coroutine_factory())
    finally:
        gemini_service.genai.Client = original_client
        os.environ.pop("GEMINI_API_KEY", None)


class ScheduleQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self._old_api_key = os.environ.pop("GEMINI_API_KEY", None)

    def tearDown(self) -> None:
        if self._old_api_key is not None:
            os.environ["GEMINI_API_KEY"] = self._old_api_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)

    def test_01_route_check_room_availability(self) -> None:
        decision = ScheduleQueryDecision(
            action="check_room_availability",
            room="c9",
            day="Monday",
            time_slot="10h15-11h45",
            confidence=0.95,
        )
        result = _with_gemini_decision(
            decision,
            lambda: schedule_query.route_schedule_query(
                "Is C9 free on Monday at 10h15-11h45?", known_rooms=["C9"]
            ),
        )
        self.assertEqual(result["source"], "gemini")
        routed = result["decision"]
        self.assertEqual(routed["action"], "check_room_availability")
        self.assertEqual(routed["room"], "C9")
        self.assertEqual(routed["day"], "lundi")
        self.assertEqual(routed["time_slot"], "10:15-11:45")
        self.assertIsNone(result["note"])

    def test_02_route_find_available_rooms(self) -> None:
        decision = ScheduleQueryDecision(
            action="find_available_rooms",
            day="mardi",
            time_slot="08:30-10:00",
            week_badge="Q1",
            group="G1",
            confidence=0.93,
        )
        result = _with_gemini_decision(
            decision,
            lambda: schedule_query.route_schedule_query(
                "Find free rooms on Tuesday morning in Q1", known_rooms=["C9"]
            ),
        )
        self.assertEqual(result["source"], "gemini")
        routed = result["decision"]
        self.assertEqual(routed["action"], "find_available_rooms")
        self.assertIsNone(routed["room"])
        self.assertEqual(routed["week_badge"], "Q1")
        self.assertEqual(routed["group"], "G1")

    def test_03_route_query_schedule(self) -> None:
        decision = ScheduleQueryDecision(
            action="query_schedule",
            teacher="Dupont",
            subject="Réseaux",
            confidence=0.91,
        )
        result = _with_gemini_decision(
            decision,
            lambda: schedule_query.route_schedule_query(
                "What does teacher Dupont teach?", known_rooms=["C9"]
            ),
        )
        self.assertEqual(result["source"], "gemini")
        routed = result["decision"]
        self.assertEqual(routed["action"], "query_schedule")
        self.assertEqual(routed["teacher"], "Dupont")
        self.assertEqual(routed["subject"], "Réseaux")

    def test_04_missing_room_is_coerced(self) -> None:
        decision = ScheduleQueryDecision(
            action="check_room_availability",
            day="lundi",
            time_slot="10:15-11:45",
            confidence=0.9,
        )
        result = _with_gemini_decision(
            decision,
            lambda: schedule_query.route_schedule_query(
                "Check room availability on Monday at 10h15-11h45",
                known_rooms=["C9"],
            ),
        )
        self.assertEqual(result["decision"]["action"], "find_available_rooms")
        self.assertIn("no room", result["note"])
        self.assertEqual(result["source"], "gemini")

        # Without any time/day context it becomes a general schedule query.
        decision = ScheduleQueryDecision(
            action="check_room_availability", confidence=0.9
        )
        result = _with_gemini_decision(
            decision,
            lambda: schedule_query.route_schedule_query(
                "Check a room", known_rooms=["C9"]
            ),
        )
        self.assertEqual(result["decision"]["action"], "query_schedule")

    def test_05_low_confidence_uses_deterministic_fallback(self) -> None:
        decision = ScheduleQueryDecision(
            action="find_available_rooms",
            room="Q2",
            confidence=0.2,
        )
        result = _with_gemini_decision(
            decision,
            lambda: schedule_query.route_schedule_query(
                "Is room C9 free on Monday?", known_rooms=["C9"]
            ),
        )
        self.assertEqual(result["source"], "fallback")
        self.assertEqual(result["model"], "fallback")
        self.assertEqual(result["decision"]["action"], "check_room_availability")
        self.assertEqual(result["decision"]["room"], "C9")
        self.assertEqual(result["decision"]["day"], "lundi")

    def test_06_without_api_key_falls_back_deterministically(self) -> None:
        result = asyncio.run(
            schedule_query.route_schedule_query(
                "Find available rooms on Tuesday 8h30-10h00", known_rooms=["C9"]
            )
        )
        self.assertEqual(result["source"], "fallback")
        self.assertIn("GEMINI_API_KEY", result["error"])
        self.assertEqual(result["decision"]["action"], "find_available_rooms")
        self.assertEqual(result["decision"]["day"], "mardi")
        self.assertEqual(result["decision"]["time_slot"], "08:30-10:00")

    def test_07_q2_is_badge_not_physical_room(self) -> None:
        result = asyncio.run(
            schedule_query.route_schedule_query(
                "Is Q2 free on Monday?", known_rooms=["C9"]
            )
        )
        routed = result["decision"]
        self.assertIsNone(routed["room"])
        self.assertEqual(routed["week_badge"], "Q2")
        self.assertEqual(routed["group"], "G2")
        self.assertEqual(routed["action"], "find_available_rooms")

    def test_08_execute_applies_alternating_week(self) -> None:
        captured: dict[str, object] = {}

        async def fake_fetch(day, slots, active_week=None):
            captured.update({"day": day, "slots": slots, "active_week": active_week})
            return [{**SAMPLE_SESSION, "room": "I1"}]

        decision = {
            "action": "check_room_availability",
            "room": "C9",
            "date": "2026-09-14",  # one week after the 2026-09-07 Q1 anchor
            "time_slot": "10:15-11:45",
            "confidence": 0.95,
        }
        with patch.object(schedule_query, "fetch_occupancies", fake_fetch):
            result = asyncio.run(
                schedule_query.execute_schedule_query(
                    decision,
                    "Is C9 free on 2026-09-14 at 10:15-11:45?",
                    known_rooms=["C9", "I1"],
                )
            )

        self.assertEqual(captured["active_week"], "Q2")
        self.assertEqual(captured["day"], "lundi")
        self.assertEqual(result["filters"]["active_week"], "Q2")
        self.assertEqual(result["filters"]["week_start"], "2026-09-07")
        self.assertTrue(result["available"])
        self.assertEqual(result["conflicts"], [])
        self.assertIn("C9", result["answer"])

    def test_09_endpoint_returns_structured_response(self) -> None:
        routed = {
            "decision": {
                "action": "check_room_availability",
                "room": "C9",
                "day": "lundi",
                "time_slot": "10:15-11:45",
                "confidence": 0.95,
            },
            "source": "fallback",
            "model": "fallback",
            "error": "test",
            "note": None,
        }

        async def fake_fetch(day, slots, active_week=None):
            return [SAMPLE_SESSION]

        with (
            patch.object(schedules_route, "load_known_rooms", return_value=["C9"]),
            patch.object(schedules_route, "route_schedule_query", return_value=routed),
            patch.object(schedules_route, "get_week_start", return_value=None),
            patch.object(schedule_query, "fetch_occupancies", fake_fetch),
        ):
            response = self.client.post(
                "/api/schedules/ask",
                json={"prompt": "Is C9 free on Monday at 10:15-11:45?"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["action"], "check_room_availability")
        self.assertFalse(body["available"])
        self.assertEqual(body["filters"]["room"], "C9")
        self.assertEqual(len(body["conflicts"]), 1)
        self.assertEqual(body["matching_sessions"][0]["subject"], "Réseaux")
        self.assertEqual(body["source"], "fallback")
        self.assertEqual(body["routing_error"], "test")


if __name__ == "__main__":
    unittest.main()

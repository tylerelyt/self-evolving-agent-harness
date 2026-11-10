"""Regression checks for reading patrol state across lab environments."""

from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from patrol import connect, publish, read_status


def metrics(environment_id):
    return {"source": "live_lab_http_requests", "environment_id": environment_id, "requests": 12,
            "error_rate": 0, "p95_ms": 1, "window_end": 30}


class StatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "patrol"
        self.environment = self.root / "environment"

    def test_unstarted_cli_does_not_create_state_or_database(self):
        cli = Path(__file__).with_name("patrol.py")
        output = subprocess.check_output([
            sys.executable, "-E", "-S", "-B", str(cli), "--state", str(self.state), "status",
            "--environment-state", str(self.environment),
        ], text=True)
        status = json.loads(output)
        self.assertEqual(status["environment"], {"state": str(self.environment.resolve()), "status": "not_started"})
        self.assertIsNone(status["patrol_database"])
        self.assertIsNone(status["services"]["payments"]["checkpoint"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_old_incident_cannot_be_confused_with_a_live_checkpoint(self):
        self.state.mkdir()
        self.environment.mkdir()
        (self.environment / "environment.json").touch()
        incident = {"incident_id": "lab-payments-000000000001", "steer_item_id": 2,
                    "steer_delivery": "callback", "probe_item_id": 3, "skill_name": "oncall-alert-triage",
                    "skill_sha256": "a" * 64, "skill_item_id": 4}
        old_report = {"status": "abnormal", "summary": "incident", "history_id": None}
        current_report = {"status": "healthy", "summary": "new environment", "history_id": None}
        with closing(connect(self.state)) as db:
            publish(db, "payments", {**metrics("old"), "error_rate": .25}, old_report, "old-session", "old-turn",
                    {"id": 5}, {"id": 6}, incident, None)
            publish(db, "payments", metrics("current"), current_report, "current-session", "current-turn",
                    {"id": 7}, {"id": 8}, None, None)
        page = self.state / "pages" / incident["incident_id"] / "page.json"
        page.parent.mkdir(parents=True)
        page.touch()
        db_path = self.state / "patrol.sqlite"
        before = db_path.read_bytes()

        with patch("patrol.get_snapshot", return_value=metrics("current")):
            status = read_status(self.state, self.environment)
        payments = status["services"]["payments"]
        self.assertEqual(status["environment"]["status"], "ready")
        self.assertEqual(payments["checkpoint"]["session_id"], "current-session")
        self.assertEqual(payments["checkpoint"]["report_item_id"], 8)
        self.assertTrue(payments["checkpoint"]["matches_environment"])
        self.assertFalse(payments["last_incident"]["matches_environment"])
        self.assertEqual(payments["last_incident"]["page_receipt"], str(page))
        self.assertIsNone(payments["last_incident"]["recovered_inspection_id"])
        self.assertEqual(db_path.read_bytes(), before)
        self.assertFalse((self.environment / "patrol.sqlite").exists())

        (self.environment / "environment.json").unlink()
        disconnected = read_status(self.state, self.environment)
        self.assertEqual(disconnected["environment"]["status"], "not_started")
        self.assertIsNone(disconnected["services"]["payments"]["checkpoint"]["matches_environment"])

    def test_partial_or_mixed_environment_is_not_ready(self):
        self.environment.mkdir()
        (self.environment / "environment.json").touch()
        with patch("patrol.get_snapshot", side_effect=[metrics("a"), OSError(), metrics("a"), metrics("a")]):
            status = read_status(self.state, self.environment)
        self.assertEqual(status["environment"]["status"], "unavailable")
        self.assertEqual(status["services"]["payments"]["live"]["state"], "unavailable")

        with patch("patrol.get_snapshot", side_effect=[metrics("a"), metrics("b"), metrics("a"), metrics("a")]):
            status = read_status(self.state, self.environment)
        self.assertEqual(status["environment"]["status"], "mixed_environment")
        self.assertFalse(self.state.exists())

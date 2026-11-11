"""Do not replay recovered monitoring events when a new patrol run starts.

The source-verification tests use an explicit, completed native run:
Set ONCALL_RECOVERED_FIXTURE to a completed workbench runs/<run-id> directory
when running this module through unittest discovery.
They copy original HTTP, patrol and Hermes evidence; no report is synthesized.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench import ALIASES, Workbench
from environment import (SERVICES, acknowledge_resolution, alert_event, load_json,
                         publish_alert, read_alert, resolution_for_alert)
from patrol import connect, incidents


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ResolutionEnvelopeTests(unittest.TestCase):
    """Incomplete metadata must never suppress a genuine monitoring event."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.alert = alert_event({"service": "payments", "environment_id": "test-environment"}, emitted_at=10)
        publish_alert(self.state, self.alert)
        self.raw = self.state / "alerts" / "payments" / f"{self.alert['incident_id']}.json"
        self.original = self.raw.read_bytes()

    def test_incomplete_resolution_does_not_hide_the_original_event(self):
        target = self.state / "resolutions" / "payments" / self.raw.name
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"schema": "lab-incident-resolution-v1",
                                      "source": "lab-verified-incident-resolution",
                                      **self.alert, "resolved_at": 100, "evidence": {}}))
        self.assertIsNone(resolution_for_alert(self.state, self.alert))
        self.assertEqual(read_alert(self.state, "payments", seen_ids=set()), self.alert)
        self.assertEqual(self.raw.read_bytes(), self.original)

    def test_invalid_acknowledgement_is_removed_and_the_event_stays_pending(self):
        with self.assertRaisesRegex(ValueError, "recovery evidence"):
            acknowledge_resolution(self.state, self.alert, {})
        self.assertFalse((self.state / "resolutions" / "payments" / self.raw.name).exists())
        self.assertEqual(read_alert(self.state, "payments"), self.alert)
        self.assertEqual(self.raw.read_bytes(), self.original)

    def test_acknowledgement_cannot_change_the_immutable_monitoring_scope(self):
        for field, value in (("environment_id", "another-environment"), ("emitted_at", 11)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "original monitoring event"):
                acknowledge_resolution(self.state, {**self.alert, field: value}, {})
        self.assertEqual(read_alert(self.state, "payments"), self.alert)
        self.assertEqual(self.raw.read_bytes(), self.original)


class VerifiedRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selected = os.environ.get("ONCALL_RECOVERED_FIXTURE")
        if not selected:
            raise unittest.SkipTest("Set ONCALL_RECOVERED_FIXTURE to a completed native workbench run")
        cls.source = Path(selected).resolve()
        cls.master_temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.master_temporary.cleanup)
        cls.master = Path(cls.master_temporary.name) / "workbench"
        cls.run_name = "recorded-native-run"
        source_status = load_json(cls.source / "service.json")
        chosen = None
        for alias in ALIASES:
            worker = cls.source / "patrols" / alias
            if not (worker / "patrol.sqlite").is_file():
                continue
            with closing(connect(worker, readonly=True)) as database:
                for service in SERVICES:
                    rows = [row for row in incidents(database, service) if row["recovered_inspection_id"] is not None]
                    if rows:
                        chosen = alias, service, worker, rows[0]
                        break
            if chosen is not None:
                break
        if chosen is None:
            raise AssertionError("The explicitly selected native fixture has no recovered incident")
        cls.alias, cls.service, cls.source_worker, cls.record = chosen
        cls.incident = cls.record["incident_id"]
        cls.observation = json.loads(cls.record["observation"])
        cls.scope = cls.observation["environment_id"]
        environment = Path(source_status["environments"][cls.alias])
        source_alert = environment / "alerts" / cls.service / f"{cls.incident}.json"
        cls.alert = load_json(source_alert)
        cls.source_hashes = {
            path: digest(path) for path in (cls.source_worker / "patrol.sqlite",
                                           cls.source_worker / "hermes/state.db", source_alert)
        }
        run = cls.master / "runs" / cls.run_name
        worker = run / "patrols" / cls.alias
        (worker / "hermes").mkdir(parents=True)
        (run / "service.json").write_bytes((cls.source / "service.json").read_bytes())
        # SQLite backup copies persisted native rows. Pruning only later rows
        # keeps this regression small while preserving original evidence bytes.
        for relative in ("patrol.sqlite", "hermes/state.db"):
            source = cls.source_worker / relative
            target = worker / relative
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, \
                    closing(sqlite3.connect(target)) as copied:
                original.backup(copied)
        with closing(sqlite3.connect(worker / "patrol.sqlite")) as database:
            database.row_factory = sqlite3.Row
            last = database.execute("SELECT * FROM inspections WHERE id=?",
                                    (cls.record["recovered_inspection_id"],)).fetchone()
            cls.last = dict(last)
            database.execute("DELETE FROM incidents WHERE incident_id != ?", (cls.incident,))
            database.execute("DELETE FROM inspections WHERE id > ?", (cls.last["id"],))
            database.commit()
            database.execute("VACUUM")
        page_source = cls.source_worker / "pages" / cls.incident
        inbox = worker / "pages" / cls.incident
        inbox.mkdir(parents=True)
        for filename in ("page.json", "investigation.json"):
            (inbox / filename).write_bytes((page_source / filename).read_bytes())
        cls.page = load_json(inbox / "page.json")
        branch = load_json(inbox / "investigation.json")
        cutoff = max(cls.last["report_item_id"], cls.record["report_item_id"], branch["report_item_id"])
        with closing(sqlite3.connect(worker / "hermes/state.db")) as database:
            database.execute("DELETE FROM messages WHERE id > ?", (cutoff,))
            database.commit()
            database.execute("VACUUM")
        target_alert = cls.master / "environments" / cls.alias / "alerts" / cls.service / source_alert.name
        target_alert.parent.mkdir(parents=True)
        target_alert.write_bytes(source_alert.read_bytes())

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "source_hashes"):
            for path, expected in cls.source_hashes.items():
                if digest(path) != expected:
                    raise AssertionError(f"Test modified its original fixture: {path}")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "workbench"
        shutil.copytree(self.master, self.root)
        self.worker = self.root / "runs" / self.run_name / "patrols" / self.alias
        self.environment = self.root / "environments" / self.alias
        self.raw = self.environment / "alerts" / self.service / f"{self.incident}.json"
        self.implementation = Path(temporary.name) / "self_improvement_agent.py"
        self.implementation.write_text("def patrol_config():\n    return {}\n")
        self.workbench = Workbench(self.root, self.implementation, live=False)
        self.addCleanup(self.workbench.close)
        self.scope_patch = patch("workbench.get_snapshot", return_value={
            "source": "live_lab_http_requests", "service": self.service, "environment_id": self.scope,
        })
        self.scope_patch.start()
        self.addCleanup(self.scope_patch.stop)
        self.before = {path: digest(path) for path in
                       (self.raw, self.worker / "patrol.sqlite", self.worker / "hermes/state.db")}

    def assert_original_evidence_unchanged(self):
        for path, expected in self.before.items():
            self.assertEqual(digest(path), expected, str(path))

    def test_verified_recovery_is_idempotent_and_leaves_open_and_new_scope_events_available(self):
        result = self.workbench.acknowledge_recovered_alerts()
        self.assertEqual(result["errors"], [])
        self.assertEqual([row["incident_id"] for row in result["acknowledged"]], [self.incident])
        self.assertIsNone(read_alert(self.environment, self.service, seen_ids=set(), environment_id=self.scope))
        receipt = resolution_for_alert(self.environment, self.alert)
        self.assertIsNotNone(receipt)
        self.assertEqual(len(receipt["evidence"]["recovery_windows"]), 2)
        self.assertEqual(receipt["evidence"]["source_report_item_id"], self.record["report_item_id"])
        self.assert_original_evidence_unchanged()
        target = Path(result["acknowledged"][0]["receipt"])
        first_bytes = target.read_bytes()

        opened = alert_event({"service": self.service, "environment_id": self.scope})
        next_environment = alert_event({"service": self.service, "environment_id": "a-new-http-environment"})
        publish_alert(self.environment, opened)
        publish_alert(self.environment, next_environment)
        self.assertEqual(read_alert(self.environment, self.service, seen_ids=set(), environment_id=self.scope), opened)
        self.assertEqual(read_alert(self.environment, self.service, seen_ids=set(),
                                    environment_id="a-new-http-environment"), next_environment)
        repeated = self.workbench.acknowledge_recovered_alerts()
        self.assertEqual(repeated, {"acknowledged": [], "errors": []})
        self.assertEqual(target.read_bytes(), first_bytes)
        self.assert_original_evidence_unchanged()

    def assert_not_acknowledged(self):
        result = self.workbench.acknowledge_recovered_alerts()
        self.assertEqual(result["acknowledged"], [])
        self.assertTrue(result["errors"])
        self.assertIsNone(resolution_for_alert(self.environment, self.alert))
        self.assertEqual(read_alert(self.environment, self.service, environment_id=self.scope), self.alert)

    def test_missing_page_keeps_the_original_alert_pending(self):
        (self.worker / "pages" / self.incident / "page.json").unlink()
        self.assert_not_acknowledged()

    def test_page_must_cite_the_actual_native_branch_report(self):
        path = self.worker / "pages" / self.incident / "page.json"
        page = load_json(path)
        page["report_item_id"] += 1000000
        path.write_text(json.dumps(page))
        self.assert_not_acknowledged()

    def test_a_single_healthy_window_cannot_close_an_alert(self):
        current = json.loads(self.last["observation"])
        with closing(sqlite3.connect(self.worker / "patrol.sqlite")) as database:
            database.row_factory = sqlite3.Row
            for row in database.execute("SELECT * FROM inspections WHERE service=? AND status='healthy'",
                                        (self.service,)).fetchall():
                observation = json.loads(row["observation"])
                if (self.page["paged_at"] < observation["window_start"]
                        and observation["window_end"] < current["window_start"]):
                    database.execute("DELETE FROM inspections WHERE id=?", (row["id"],))
            database.commit()
        self.assert_not_acknowledged()

    def test_healthy_rows_need_their_original_hermes_tool_receipts(self):
        with closing(sqlite3.connect(self.worker / "hermes/state.db")) as database:
            database.execute("DELETE FROM messages WHERE id=?", (self.last["evidence_item_id"],))
            database.commit()
        self.assert_not_acknowledged()

    def test_old_environment_recovery_does_not_acknowledge_the_new_scope(self):
        with patch("workbench.get_snapshot", return_value={"source": "live_lab_http_requests",
                   "service": self.service, "environment_id": "a-new-http-environment"}):
            result = self.workbench.acknowledge_recovered_alerts()
        self.assertEqual(result, {"acknowledged": [], "errors": []})
        self.assertIsNone(resolution_for_alert(self.environment, self.alert))
        self.assert_original_evidence_unchanged()

    def test_start_agent_acknowledges_before_spawning_its_new_patrol_database(self):
        child = Mock(pid=12345)
        child.poll.return_value = 0

        def spawn(command, log):
            self.assertIsNotNone(resolution_for_alert(self.environment, self.alert))
            self.assertIsNone(read_alert(self.environment, self.service, environment_id=self.scope))
            return child

        stages = [{"number": number, "implemented": True} for number in range(1, 5)]
        with patch("workbench.stage_status", return_value=stages), \
                patch.object(self.workbench, "spawn", side_effect=spawn) as launch:
            result = self.workbench.start_agent()
        self.assertEqual(result["status"], "starting")
        launch.assert_called_once()
        self.assert_original_evidence_unchanged()


if __name__ == "__main__":
    unittest.main()

"""Regression tests for workbench evidence and read-only session inspection."""

from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from workbench import Workbench, file_hash, make_server


IMPLEMENTATION = '''
def patrol_config():
    return {"services": ["payments"], "sample": 1, "interval": 1}

def route_alert(steer, alert):
    return True

def branch_report(sessions, record):
    return "child", {}

def notify_operator(path, recipient, scope, result, evidence):
    return path

def recall_memory(state, memory, environment_state, scope, options):
    return {}

def release_policy(results, baseline_sha, candidate_sha):
    return True
'''


class WorkbenchTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.implementation = self.directory / "self_improvement_agent.py"
        self.implementation.write_text(IMPLEMENTATION, encoding="utf-8")
        self.workbench = Workbench(self.directory / "workbench", self.implementation)
        self.addCleanup(self.workbench.close)

    def snapshot(self):
        metrics = {"source": "live_lab_http_requests", "environment_id": "test-env",
                   "window_start": 100, "window_end": 130, "requests": 80,
                   "error_rate": 0, "p95_ms": 12}
        with patch("workbench.get_snapshot", return_value=metrics):
            return self.workbench.snapshot()


class CheckEvidenceTests(WorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.check_directory = self.workbench.root / "checks" / "external-cli-check"
        self.check_directory.mkdir(parents=True)
        checks = []
        for number in range(1, 4):
            filename = f"stage-{number}.json"
            (self.check_directory / filename).write_text("{}", encoding="utf-8")
            checks.append({"stage": number, "status": "passed", "evidence": [filename]})
        self.evidence = {
            "schema": "oncall-workshop-check-v1", "status": "passed", "stage": 3,
            "implementation": {"path": str(self.implementation),
                               "sha256": file_hash(self.implementation)},
            "checks": checks, "completed_at": 140,
        }

    def write_check(self, evidence=None):
        content = self.evidence if evidence is None else evidence
        (self.check_directory / "check.json").write_text(json.dumps(content), encoding="utf-8")

    def test_external_cli_check_is_discovered_without_a_registered_job(self):
        self.write_check()
        self.assertEqual(self.workbench.record["checks"], [])

        status = self.snapshot()

        self.assertEqual(len(status["checks"]), 1)
        check = status["checks"][0]
        self.assertEqual(check["id"], "external-cli-check")
        self.assertTrue(check["evidence_valid"])
        self.assertTrue(check["verified"])
        self.assertEqual(check["implementation_sha256"], file_hash(self.implementation))
        self.assertEqual([stage["verified"] for stage in status["agent"]["stages"]],
                         [True, True, True, False])

    def test_passed_metadata_without_all_stage_evidence_is_not_verified(self):
        variants = []
        metadata_only = deepcopy(self.evidence)
        metadata_only.pop("checks")
        variants.append(metadata_only)
        missing_stage = deepcopy(self.evidence)
        missing_stage["checks"].pop(1)
        variants.append(missing_stage)
        empty_evidence = deepcopy(self.evidence)
        empty_evidence["checks"][1]["evidence"] = []
        variants.append(empty_evidence)
        failed_stage = deepcopy(self.evidence)
        failed_stage["checks"][1]["status"] = "failed"
        variants.append(failed_stage)

        for evidence in variants:
            with self.subTest(evidence=evidence):
                self.write_check(evidence)
                status = self.snapshot()
                self.assertFalse(status["checks"][0]["evidence_valid"])
                self.assertFalse(status["checks"][0]["verified"])
                self.assertFalse(any(stage["verified"] for stage in status["agent"]["stages"]))

    def test_deleting_an_evidence_file_revokes_verification(self):
        self.write_check()
        self.assertTrue(self.snapshot()["checks"][0]["verified"])

        (self.check_directory / "stage-2.json").unlink()

        check = self.snapshot()["checks"][0]
        self.assertFalse(check["evidence_valid"])
        self.assertFalse(check["verified"])

    def test_invalid_schema_or_failed_check_cannot_verify_stages(self):
        for field, value in (("schema", "unrelated-check"), ("status", "failed")):
            with self.subTest(field=field):
                evidence = deepcopy(self.evidence)
                evidence[field] = value
                self.write_check(evidence)
                self.assertFalse(self.snapshot()["checks"][0]["verified"])

    def test_complete_evidence_for_other_code_does_not_verify_current_code(self):
        self.evidence["implementation"]["sha256"] = "0" * 64
        self.write_check()

        status = self.snapshot()

        self.assertTrue(status["checks"][0]["evidence_valid"])
        self.assertFalse(status["checks"][0]["verified"])
        self.assertFalse(any(stage["verified"] for stage in status["agent"]["stages"]))

    def test_editing_implementation_revokes_previous_verification(self):
        self.write_check()
        self.assertTrue(self.snapshot()["checks"][0]["verified"])

        self.implementation.write_text(IMPLEMENTATION + "\n# Revised implementation\n", encoding="utf-8")

        status = self.snapshot()
        self.assertTrue(status["checks"][0]["evidence_valid"])
        self.assertFalse(status["checks"][0]["verified"])
        self.assertFalse(any(stage["verified"] for stage in status["agent"]["stages"]))


class SessionTestCase(WorkbenchTestCase):
    def setUp(self):
        super().setUp()
        run = self.workbench.root / "runs" / "test-run"
        run.mkdir(parents=True)
        (run / "service.json").write_text("{}", encoding="utf-8")
        self.source = "test-run/instance-a"
        state = run / "patrols" / "instance-a"
        database = state / "hermes" / "state.db"
        database.parent.mkdir(parents=True)
        self.report_item = 600
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, parent_session_id TEXT,
                    started_at REAL, model TEXT, message_count INTEGER
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, tool_call_id TEXT, tool_name TEXT, tool_calls TEXT
                );
            """)
            connection.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", [
                ("patrol", None, 100, "local-test", 1200),
                ("other-session", None, 101, "local-test", 1),
            ])
            rows = [(number, "patrol", "assistant", f"message-{number}", None, None, "[]")
                    for number in range(1, 1201)]
            rows[self.report_item - 1] = (self.report_item, "patrol", "assistant", "saved incident report",
                                         None, None, "[]")
            rows.append((1201, "other-session", "assistant", "other incident", None, None, "[]"))
            connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


class SessionInspectionTests(SessionTestCase):
    def test_report_anchor_retains_report_after_hundreds_of_later_patrol_messages(self):
        result = self.workbench.session(self.source, "patrol", str(self.report_item))

        self.assertEqual(result["anchor_item_id"], self.report_item)
        self.assertEqual(result["messages"][-1]["id"], self.report_item)
        self.assertEqual(result["messages"][-1]["content"], "saved incident report")
        ids = [message["id"] for message in result["messages"]]
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(all(item <= self.report_item for item in ids))
        self.assertTrue(result["has_earlier_messages"])

    def test_unanchored_inspection_shows_latest_messages(self):
        result = self.workbench.session(self.source, "patrol")

        self.assertIsNone(result["anchor_item_id"])
        self.assertEqual(result["messages"][-1]["id"], 1200)
        self.assertTrue(result["has_earlier_messages"])

    def test_unknown_source_or_session_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "source"):
            self.workbench.session("unlisted/instance-a", "patrol", self.report_item)
        with self.assertRaisesRegex(ValueError, "session"):
            self.workbench.session(self.source, "unknown-session", self.report_item)

    def test_item_from_another_session_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Item"):
            self.workbench.session(self.source, "patrol", 1201)

    def test_missing_or_nonnumeric_item_is_rejected(self):
        for item in (99999, "not-an-item"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.workbench.session(self.source, "patrol", item)


class WorkbenchHTTPTests(SessionTestCase):
    def setUp(self):
        super().setUp()
        html = self.directory / "index.html"
        html.write_text("<p>Workbench test</p>", encoding="utf-8")
        self.server = make_server(self.workbench, "127.0.0.1", 0, html)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.shutdown)
        self.authority = f"127.0.0.1:{self.server.server_port}"

    def request(self, method, path, body=None, origin=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_cross_origin_action_is_rejected_before_dispatch(self):
        with patch.object(self.workbench, "action", return_value={"status": "accepted"}) as action:
            status, result = self.request("POST", "/api/actions", json.dumps({"action": "agent_stop"}),
                                          origin="https://other.example")
        self.assertEqual(status, 403)
        self.assertIn("error", result)
        action.assert_not_called()

    def test_same_origin_action_reaches_dispatch(self):
        body = {"action": "agent_stop"}
        with patch.object(self.workbench, "action", return_value={"status": "accepted"}) as action:
            status, result = self.request("POST", "/api/actions", json.dumps(body),
                                          origin=f"http://{self.authority}")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"status": "accepted"})
        action.assert_called_once_with(body)

    def test_session_endpoint_preserves_report_anchor(self):
        query = urlencode({"source": self.source, "session": "patrol", "item": self.report_item})
        status, result = self.request("GET", "/api/session?" + query)
        self.assertEqual(status, 200)
        self.assertEqual(result["anchor_item_id"], self.report_item)
        self.assertEqual(result["messages"][-1]["content"], "saved incident report")

    def test_session_endpoint_rejects_missing_source_and_foreign_item(self):
        invalid_queries = [{"session": "patrol", "item": self.report_item},
                           {"source": self.source, "session": "patrol", "item": 1201}]
        for parameters in invalid_queries:
            with self.subTest(parameters=parameters):
                status, result = self.request("GET", "/api/session?" + urlencode(parameters))
                self.assertEqual(status, 400)
                self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()

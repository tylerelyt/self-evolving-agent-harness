"""Keep business logs observational and configuration history attributable."""

from contextlib import ExitStack, closing, contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from environment import (CHANGE_SOURCE, Handler, REQUEST_LOG_SOURCE, Service,
                         get_request_logs, get_snapshot, inject_fault,
                         read_changes, write_json)


PATROL = Path(__file__).resolve().parent


class RequestObservabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.scope = "observability-test-environment"
        write_json(self.state / "environment-scope.json", {"environment_id": self.scope})

    @contextmanager
    def http_service(self):
        service = Service("payments", self.state, self.scope)
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.service = service
            write_json(self.state / "environment.json", {
                "payments": f"http://127.0.0.1:{server.server_port}",
            })
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                yield service, server.server_port
            finally:
                server.shutdown()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())

    def work(self, port, *, probe=False):
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
            connection.request("GET", "/work", headers={"X-Lab-Probe": "1" if probe else "0"})
            with connection.getresponse() as response:
                return response.status, response.getheader("X-Lab-Request-Id"), json.load(response)

    def test_actual_http_success_and_failure_have_correlated_request_logs(self):
        with self.http_service() as (_service, port):
            success = self.work(port)
            inject_fault(self.state, "payments", "on", actor="test-engineer")
            failure = self.work(port, probe=True)
            snapshot = get_snapshot(self.state, "payments")
            result = get_request_logs(self.state, "payments")

        self.assertEqual((success[0], failure[0]), (200, 503))
        self.assertEqual(result["source"], REQUEST_LOG_SOURCE)
        self.assertEqual((result["service"], result["environment_id"]), ("payments", self.scope))
        self.assertEqual(len(result["entries"]), snapshot["requests"])
        self.assertEqual(len(result["entries"]), 2)
        for expected, entry, is_probe in zip((success, failure), result["entries"], (False, True)):
            self.assertRegex(entry["id"], r"^request-[0-9a-f]{32}$")
            self.assertEqual(entry["id"], expected[1])
            self.assertEqual(entry["status"], expected[0])
            self.assertEqual(entry["probe"], is_probe)
            self.assertEqual((entry["method"], entry["path"]), ("GET", "/work"))
            self.assertEqual((entry["service"], entry["environment_id"]), ("payments", self.scope))
            self.assertGreater(entry["duration_ms"], 0)
            self.assertLessEqual(entry["duration_ms"], 3000)
            self.assertLessEqual(result["window_start"], entry["time"])
            self.assertLessEqual(entry["time"], result["window_end"])
        self.assertNotEqual(success[1], failure[1])

    def test_reading_logs_and_metrics_does_not_create_work_or_advance_probe_sequence(self):
        with self.http_service() as (service, port):
            inject_fault(self.state, "payments", "intermittent")
            first = self.work(port, probe=True)
            for _ in range(5):
                self.assertEqual(len(get_request_logs(self.state, "payments")["entries"]), 1)
                self.assertEqual(get_snapshot(self.state, "payments")["requests"], 1)
                self.assertEqual(service.probe_counts, {"payments": 1})
                self.assertEqual(service.traffic_counts, {})
            second = self.work(port, probe=True)
            result = get_request_logs(self.state, "payments")

        self.assertEqual((first[0], second[0]), (503, 200))
        self.assertEqual([entry["id"] for entry in result["entries"]], [first[1], second[1]])
        self.assertEqual([entry["status"] for entry in result["entries"]], [503, 200])

    def test_request_history_is_bounded_and_expires_without_losing_recent_receipts(self):
        with patch("environment.MAX_TELEMETRY_ROWS", 3):
            service = Service("payments", self.state, self.scope)
        # The requests execute normally; only the observation clock is controlled.
        for timestamp in (1000, 1001, 1002, 1003):
            with patch("environment.time.time", return_value=timestamp):
                service.request()
        with patch("environment.time.time", return_value=1004):
            entries = service.request_log()["entries"]
        self.assertEqual([entry["time"] for entry in entries], [1001, 1002, 1003])
        self.assertEqual(len({entry["id"] for entry in entries}), 3)
        with patch("environment.time.time", return_value=1242):
            self.assertEqual([entry["time"] for entry in service.request_log()["entries"]], [1002, 1003])
        with patch("environment.time.time", return_value=1244):
            self.assertEqual(service.request_log()["entries"], [])

    def test_changes_retain_scope_actual_actor_and_marker_transitions(self):
        inject_fault(self.state, "payments", "on", actor="engineer-a")
        inject_fault(self.state, "payments", "intermittent", actor="engineer-b")
        inject_fault(self.state, "payments", "off", actor="engineer-c")
        inject_fault(self.state, "payments", "off", actor="nothing-changed")
        write_json(self.state / "environment-scope.json", {"environment_id": "next-environment"})
        inject_fault(self.state, "payments", "on", actor="engineer-d")

        original = read_changes(self.state, environment_id=self.scope)
        current = read_changes(self.state, environment_id="next-environment")
        self.assertEqual([(row["before"], row["after"]) for row in original], [
            ("off", "on"), ("on", "intermittent"), ("intermittent", "off"),
        ])
        self.assertEqual([row["actor"] for row in original], ["engineer-a", "engineer-b", "engineer-c"])
        self.assertTrue(all(row["type"] == "fault_changed" and row["source"] == CHANGE_SOURCE
                            and row["service"] == "payments" and row["summary"] for row in original))
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["actor"], "engineer-d")
        self.assertEqual(read_changes(self.state, limit=2), [original[-1], current[0]])
        self.assertEqual(len(read_changes(self.state)), 4)

    def test_concurrent_processes_preserve_all_changes_and_before_after_order(self):
        script = '''
from pathlib import Path
import sys
from environment import inject_fault
state, actor = Path(sys.argv[1]), sys.argv[2]
for count in range(12):
    inject_fault(state, "payments", "on" if count % 2 == 0 else "intermittent", actor=actor)
'''
        children = []
        try:
            for number in range(4):
                children.append(subprocess.Popen(
                    [sys.executable, "-E", "-S", "-B", "-c", script, str(self.state), f"writer-{number}"],
                    cwd=PATROL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            while any(child.poll() is None for child in children):
                # A dashboard reader must never see a partial JSONL row.
                for row in read_changes(self.state):
                    self.assertEqual(row["source"], CHANGE_SOURCE)
                time.sleep(.005)
            for child in children:
                output, error = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, output + error)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

        rows = read_changes(self.state)
        self.assertEqual(len(rows), 48)
        self.assertEqual(len({row["id"] for row in rows}), 48)
        self.assertEqual({row["actor"] for row in rows}, {f"writer-{number}" for number in range(4)})
        self.assertEqual(rows[0]["before"], "off")
        for earlier, later in zip(rows, rows[1:]):
            self.assertEqual(later["before"], earlier["after"])
            self.assertLessEqual(earlier["time"], later["time"])
        self.assertEqual((self.state / "payments.fault").read_text(), rows[-1]["after"])
        parsed_lines = [json.loads(line) for line in (self.state / "changes.jsonl").read_text().splitlines()]
        self.assertEqual(parsed_lines, rows)

    def test_exclusive_fault_cleanup_preserves_an_existing_marker_and_records_its_own_recovery(self):
        inject_fault(self.state, "orders", "intermittent", actor="other-owner")
        with self.assertRaises(FileExistsError), ExitStack() as cleanup:
            inject_fault(self.state, "payments", "on", cleanup=cleanup, actor="our-owner")
            inject_fault(self.state, "orders", "on", cleanup=cleanup, actor="our-owner")

        self.assertFalse((self.state / "payments.fault").exists())
        self.assertEqual((self.state / "orders.fault").read_text(), "intermittent")
        rows = read_changes(self.state)
        self.assertEqual([(row["service"], row["actor"], row["before"], row["after"]) for row in rows], [
            ("orders", "other-owner", "off", "intermittent"),
            ("payments", "our-owner", "off", "on"),
            ("payments", "lab-cleanup", "on", "off"),
        ])

    def test_environment_start_records_its_actual_published_scope(self):
        process = subprocess.Popen(
            [sys.executable, "-E", "-S", "-B", str(PATROL / "environment.py"),
             "--state", str(self.state), "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                self.assertIsNone(process.poll(), "Environment exited before publishing HTTP endpoints")
                if (self.state / "environment.json").is_file():
                    try:
                        snapshot = get_snapshot(self.state, "payments")
                        if snapshot["requests"]:
                            break
                    except (OSError, ValueError, KeyError):
                        pass
                if time.monotonic() >= deadline:
                    self.fail("Environment never published a live HTTP metric window")
                time.sleep(.02)
            rows = read_changes(self.state, environment_id=snapshot["environment_id"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["type"], "environment_started")
            self.assertEqual((rows[0]["service"], rows[0]["before"], rows[0]["after"]), (None, None, "running"))
            self.assertEqual(rows[0]["actor"], "lab-environment")
            self.assertNotEqual(snapshot["environment_id"], self.scope)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                _output, error = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
                raise
        self.assertEqual(process.returncode, 0, error)
        self.assertFalse((self.state / "environment.json").exists())


if __name__ == "__main__":
    unittest.main()

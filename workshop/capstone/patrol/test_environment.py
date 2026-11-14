"""Regression checks for local telemetry and fault ownership."""

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from environment import (LoopbackHTTPServer, alert_event, get_snapshot, get_telemetry, monitor_alerts, probe_service,
                         publish_alert, read_alert, write_json)
from fleet import set_faults


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def endpoint(self, address):
        write_json(self.state / "environment.json", {"payments": address})

    def alert(self, suffix, emitted_at, environment="current"):
        return {**alert_event({"service": "payments", "environment_id": environment}, emitted_at=emitted_at),
                "incident_id": f"lab-payments-{suffix:012x}"}

    def test_pending_events_survive_newer_incidents_and_filter_their_scope(self):
        old_environment = self.alert(1, 1, "previous")
        first = self.alert(2, 2)
        second = self.alert(3, 3)
        for event in (old_environment, first, second):
            publish_alert(self.state, event)

        self.assertEqual(read_alert(self.state, "payments"), second)
        self.assertEqual(read_alert(self.state, "payments", seen_ids=set(), environment_id="current"), first)
        self.assertEqual(read_alert(self.state, "payments", seen_ids={first["incident_id"]},
                                    environment_id="current"), second)
        self.assertIsNone(read_alert(self.state, "payments", environment_id="current",
                                     seen_ids={first["incident_id"], second["incident_id"]}))
        self.assertEqual(len(list((self.state / "alerts/payments").glob("*.json"))), 3)

    def test_legacy_event_is_still_available_without_a_queue(self):
        event = self.alert(1, 1)
        write_json(self.state / "payments.alert.json", event)
        self.assertEqual(read_alert(self.state, "payments"), event)
        self.assertEqual(read_alert(self.state, "payments", seen_ids=set(), environment_id="current"), event)
        self.assertIsNone(read_alert(self.state, "payments", seen_ids={event["incident_id"]}))
        self.assertIsNone(read_alert(self.state, "payments", environment_id="previous"))

    def test_republishing_an_incident_cannot_change_its_evidence(self):
        event = self.alert(1, 1)
        publish_alert(self.state, event)
        publish_alert(self.state, dict(event))
        with self.assertRaisesRegex(RuntimeError, "different monitoring event"):
            publish_alert(self.state, {**event, "environment_id": "different"})
        self.assertEqual(read_alert(self.state, "payments"), event)
        self.assertEqual(list((self.state / "alerts/payments").iterdir()),
                         [self.state / "alerts/payments" / f"{event['incident_id']}.json"])

    def test_concurrent_publishers_preserve_all_incidents(self):
        events = [self.alert(number, number) for number in range(1, 13)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda event: publish_alert(self.state, event), events + events))
        self.assertEqual(len(list((self.state / "alerts/payments").glob("*.json"))), len(events))
        self.assertEqual(read_alert(self.state, "payments"), events[-1])
        self.assertEqual(read_alert(self.state, "payments", seen_ids=set()), events[0])

    def test_monitor_retries_the_same_event_if_legacy_publication_fails(self):
        (self.state / "payments.fault").touch()
        observations = {"service": "payments", "environment_id": "current", "error_rate": .5}
        writes = []

        def flaky_write(path, value):
            writes.append(value)
            if len(writes) == 1:
                raise OSError("Controlled latest-event write failure")
            write_json(path, value)

        with patch("environment.get_snapshot", return_value=observations), \
                patch("environment.write_json", side_effect=flaky_write):
            stop = unittest.mock.Mock()
            stop.wait.side_effect = [False, False, False, True]
            monitor_alerts(self.state, stop)

        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], writes[1])
        self.assertEqual(len(list((self.state / "alerts/payments").glob("*.json"))), 1)
        self.assertEqual(read_alert(self.state, "payments"), writes[0])

    def test_rejects_external_address_before_opening_http(self):
        self.endpoint("http://example.invalid:8080")
        with patch("environment.build_opener") as opener:
            for operation in (get_snapshot, get_telemetry, probe_service):
                with self.subTest(operation=operation.__name__), self.assertRaisesRegex(RuntimeError, "loopback"):
                    operation(self.state, "payments")
            opener.assert_not_called()

    def test_rejects_redirects_but_retains_scoped_fault_receipts(self):
        requests = []
        redirect = True

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if redirect and self.path != "/redirect-target":
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                    return
                self.send_response(503)
                self.send_header("X-Lab-Trace-Id", "a" * 32)
                self.end_headers()
                self.wfile.write(json.dumps({"service": "payments", "environment_id": "lab"}).encode())

            def log_message(self, *_args):
                pass

        with LoopbackHTTPServer(("127.0.0.1", 0), Handler) as server:
            self.endpoint(f"http://127.0.0.1:{server.server_port}")
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                for operation in (get_snapshot, get_telemetry, probe_service):
                    with self.assertRaisesRegex(RuntimeError, "redirect"):
                        operation(self.state, "payments")
                redirect = False
                receipt = probe_service(self.state, "payments", environment_id="lab")
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual(requests, ["/metrics", "/telemetry", "/work", "/work"])
        self.assertEqual((receipt["http_status"], receipt["trace_id"], receipt["environment_id"]), (503, "a" * 32, "lab"))

    def test_partial_fault_injection_cleans_only_the_new_marker(self):
        environments = {alias: self.state / alias for alias in ("instance-a", "instance-b")}
        for directory in environments.values():
            directory.mkdir()
        existing = environments["instance-b"] / "orders.fault"
        existing.write_text("intermittent", encoding="utf-8")

        with self.assertRaises(FileExistsError), ExitStack() as cleanup:
            set_faults(environments, dict.fromkeys(environments, "on"), cleanup=cleanup)

        self.assertFalse((environments["instance-a"] / "payments.fault").exists())
        self.assertEqual(existing.read_text(encoding="utf-8"), "intermittent")

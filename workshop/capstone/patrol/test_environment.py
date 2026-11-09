"""Regression checks for local telemetry and fault ownership."""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from environment import get_snapshot, get_telemetry, probe_service, write_json
from fleet import set_faults


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def endpoint(self, address):
        write_json(self.state / "environment.json", {"payments": address})

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

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
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

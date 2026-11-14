"""Real loopback HTTP remains usable when reverse DNS is unavailable."""

from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from self_evolving_harness.http_server import LoopbackHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"ready"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LoopbackHTTPTests(unittest.TestCase):
    def test_real_http_does_not_require_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("DNS is unavailable")):
            with LoopbackHTTPServer(("127.0.0.1", 0), Handler) as server:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertEqual(server.server_port, server.socket.getsockname()[1])
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}/health"
                    with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
                        self.assertEqual((response.status, response.read()), (200, b"ready"))
                finally:
                    server.shutdown()
                    thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_only_numeric_ipv4_loopback_is_accepted(self):
        for host in ("", "localhost", "0.0.0.0", "::1", "192.0.2.1"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "numeric loopback"):
                LoopbackHTTPServer((host, 0), Handler)


if __name__ == "__main__":
    unittest.main()

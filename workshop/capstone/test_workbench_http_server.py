"""Real model-fixture HTTP remains usable while reverse DNS is unavailable."""

import json
from http.server import BaseHTTPRequestHandler
import unittest
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler, Request

from http_server import LoopbackHTTPServer
from local_model import serve


class ModelTransportTests(unittest.TestCase):
    def test_real_model_fixture_does_not_require_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("DNS is unavailable")):
            with serve(lambda messages: (None, {"source": "local HTTP"})) as url:
                payload = json.dumps({"model": "fixture", "messages": []}).encode()
                request = Request(url + "/chat/completions", data=payload,
                                  headers={"Content-Type": "application/json"})
                with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
                    answer = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(answer["choices"][0]["message"]["content"]),
                                     {"source": "local HTTP"})

    def test_only_numeric_ipv4_loopback_is_accepted(self):
        for host in ("", "localhost", "0.0.0.0", "::1", "192.0.2.1"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "numeric loopback"):
                LoopbackHTTPServer((host, 0), BaseHTTPRequestHandler)


if __name__ == "__main__":
    unittest.main()

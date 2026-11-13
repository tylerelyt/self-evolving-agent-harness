"""Real loopback transport checks for bounded model acceptance."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall.acceptance import budget_proxy


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, content_type):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


class TransportTests(unittest.TestCase):
    def request(self, url, token, *, model="fixed-model", stream=False):
        request = Request(url + "/chat/completions", data=json.dumps({"model": model, "stream": stream,
            "messages": [{"role": "user", "content": "private prompt"}]}).encode(),
            headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers.get_content_type()

    def test_reserve_budget_before_network_and_keep_secrets_out_of_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "usage.json"
            upstream = []

            def open_upstream(request, **kwargs):
                upstream.append(request)
                return Response(b'{"usage":{"total_tokens":7}}', "application/json")

            with patch("apps.oncall.acceptance.build_opener") as factory:
                factory.return_value.open.side_effect = open_upstream
                with budget_proxy("https://fixed.example/v1", "private-upstream-key", "fixed-model", ledger,
                                  max_requests=1) as (url, token):
                    for credential, model, status in [("wrong", "fixed-model", 401), (token, "other-model", 400)]:
                        with self.assertRaises(HTTPError) as error:
                            self.request(url, credential, model=model)
                        self.assertEqual(error.exception.code, status)
                    self.assertEqual(self.request(url, token)[0], 200)
                    with self.assertRaises(HTTPError) as error:
                        self.request(url, token)
                    self.assertEqual(error.exception.code, 429)
            self.assertEqual(len(upstream), 1)
            self.assertEqual(upstream[0].full_url, "https://fixed.example/v1/chat/completions")
            self.assertEqual(upstream[0].get_header("Authorization"), "Bearer private-upstream-key")
            data = json.loads(ledger.read_text())
            self.assertEqual(data["requests"][0]["usage"], {"total_tokens": 7})
            for secret in ("private-upstream-key", "private prompt", token):
                self.assertNotIn(secret, ledger.read_text())

    def test_native_stream_is_preserved_and_final_usage_recorded(self):
        body = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: {"usage":{"total_tokens":9}}\n\ndata: [DONE]\n\n'
        with tempfile.TemporaryDirectory() as directory, patch("apps.oncall.acceptance.build_opener") as factory:
            ledger = Path(directory) / "usage.json"
            factory.return_value.open.return_value = Response(body, "text/event-stream")
            with budget_proxy("https://fixed.example", "key", "fixed-model", ledger) as (url, token):
                status, actual, content_type = self.request(url, token, stream=True)
            self.assertEqual((status, actual, content_type), (200, body, "text/event-stream"))
            self.assertEqual(json.loads(ledger.read_text())["requests"][0]["usage"]["total_tokens"], 9)


if __name__ == "__main__":
    unittest.main()

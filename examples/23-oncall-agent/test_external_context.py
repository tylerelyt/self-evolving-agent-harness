"""Frozen-context controls, plus native OpenViking checks when its interpreter is supplied."""
import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest

import external_context as context


def constructed_bundle():
    text = context.RUNBOOK.read_text()
    bundle = {"schema": context.SCHEMA, "provider": "openviking", "scope": context.SCOPE,
              "endpoint": "http://127.0.0.1:1", "identity": {"account": "fixture", "user": "fixture"},
              "namespace_uri": "viking://resources/lesson23-fixture", "native_receipts_sha256": context.digest([]),
              "entries": [{"uri": "viking://resources/lesson23-fixture/payment-runbook.md",
                           "scope": context.SCOPE, "kind": "runbook", "is_current_observation": False,
                           "content": text, "content_sha256": context.content_hash(text), "receipts": []}]}
    bundle["context_sha256"] = context.digest(bundle)
    return bundle


class FrozenContextTests(unittest.TestCase):
    def test_scope_and_content_are_checked_before_using_context(self):
        bundle = constructed_bundle()
        with self.assertRaises(ValueError):
            context.read_context(bundle, "other-service", "lab")
        changed = copy.deepcopy(bundle)
        changed["entries"][0]["content"] += "changed"
        with self.assertRaises(ValueError):
            context.read_context(changed, "payment-api", "lab")
        changed["context_sha256"] = context.digest({key: value for key, value in changed.items() if key != "context_sha256"})
        with self.assertRaises(ValueError):
            context.read_context(changed, "payment-api", "lab")

    def test_new_old_reads_are_equal_and_do_not_share_mutable_entries(self):
        bundle = constructed_bundle()
        old = context.read_context(bundle, "payment-api", "lab")
        new = context.read_context(bundle, "payment-api", "lab")
        self.assertEqual(old, new)
        old["entries"][0]["content"] = "local caller changed its own copy"
        self.assertNotEqual(old, context.read_context(bundle, "payment-api", "lab"))

    def test_resource_namespace_boundary_and_receipt_integrity(self):
        self.assertFalse(context.within_namespace("viking://resources/lesson23-other/file", "viking://resources/lesson23"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context.save(root / "external-context.json", constructed_bundle())
            context.save(root / "native-receipts.json", [])
            self.assertEqual(context.load_frozen_context(root / "external-context.json")["scope"], context.SCOPE)
            context.save(root / "native-receipts.json", [{"response": "tampered"}])
            with self.assertRaisesRegex(ValueError, "retrieval evidence"):
                context.load_frozen_context(root / "external-context.json")

    @unittest.skipUnless(os.environ.get("OPENVIKING_PYTHON"), "set OPENVIKING_PYTHON for native local-service checks")
    def test_real_service_online_queries_and_frozen_evaluation(self):
        with tempfile.TemporaryDirectory(prefix="lesson23-native-context-") as temporary:
            root = Path(temporary)
            with context.scripted_service(root / "service") as endpoint:
                bundle = context.prepare_context(root / "context", endpoint=endpoint)
                frozen = context.load_frozen_context(root / "context/external-context.json")
                online = context.live_read_context(bundle, "payment-api", "lab")
                offline = context.read_context(frozen, "payment-api", "lab")
                self.assertEqual(online["entries"], offline["entries"])
                self.assertEqual([row["operation"] for row in online["native_receipts"]], ["find", "read"])
                for row in online["native_receipts"]:
                    self.assertEqual(row["response"]["status"], "ok")
                native = context.client(endpoint, **bundle["identity"])
                entry = bundle["entries"][0]
                native.post("/api/v1/content/write", {"uri": entry["uri"], "content": entry["content"] + "\nchanged after freeze\n",
                                                      "mode": "replace", "wait": True, "timeout": 90}, timeout=100)
                with self.assertRaisesRegex(ValueError, "changed from"):
                    context.live_read_context(bundle, "payment-api", "lab")
                self.assertEqual(context.read_context(frozen, "payment-api", "lab"), offline)
            receipt = json.loads((root / "service/service.json").read_text())
            self.assertEqual(receipt["glm_requests"], 0)
            self.assertGreater(receipt["fixture_requests"]["embedding_requests"], 0)
            self.assertGreater(receipt["fixture_requests"]["summary_requests"], 0)
            parsed = context.urlsplit(endpoint)
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex((parsed.hostname, parsed.port)), 0)


if __name__ == "__main__":
    unittest.main()

"""Application memory binding and provenance; provider here is a unit fixture."""

import json
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall import memory
from self_evolving_harness.knowledge import KnowledgeStore, content_digest


class ProviderFixture:
    def __init__(self):
        self.records = {}
        self.calls = 0
        self.unavailable = False

    def put_revision(self, record):
        self.records[record["revision_id"]] = record
        return {"revision_id": record["revision_id"], "uri": "viking://unit/" + record["revision_id"],
                "content_sha256": content_digest(record)}

    def search(self, scope, query, limit=10):
        self.calls += 1
        if self.unavailable:
            raise ConnectionError("unit provider outage")
        return [self.put_revision(record) for record in list(self.records.values()) if record["scope"] == scope][:limit]


class OncallMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = self.root / "template.json"
        self.template.write_text(json.dumps({"schema_version": 1, "backend": "openviking",
            "endpoint": "http://127.0.0.1:19537", "tenant": "test-team", "state_dir": "knowledge"}))
        self.ids = {"instance-a": "a" * 32, "instance-b": "b" * 32}
        self.bound = memory.bind_configuration(self.template, self.root / "runtime", self.ids)
        self.config = memory.load_binding(self.bound)
        self.scope = {"tenant": "test-team", "environment": "a" * 32, "service": "payments"}
        self.store = KnowledgeStore(Path(self.config["state_dir"]) / "knowledge.sqlite3")
        self.backend = ProviderFixture()
        self.sources = [{"session_id": "native-source", "item_id": 5, "sha256": "c" * 64}]

    def test_binding_namespace_survives_restart_and_rejects_another_tenant(self):
        again = memory.bind_configuration(self.template, self.root / "runtime", self.ids)
        self.assertEqual(memory.load_binding(again)["namespace"], self.config["namespace"])
        data = json.loads(self.template.read_text())
        data["tenant"] = "another-team"
        self.template.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "different deployment"):
            memory.bind_configuration(self.template, self.root / "runtime", self.ids)

    def test_provider_outage_never_reuses_local_facts_and_wrong_scope_is_denied(self):
        self.store.revise(scope=self.scope, key="incident:one", content={"observed": "historical"}, sources=self.sources)
        self.store.drain(self.backend)
        with patch.dict(os.environ, {"ONCALL_MEMORY_CONFIG": str(self.bound)}), patch.object(memory, "provider", return_value=self.backend):
            tool = memory.configured_lookup(root=self.root, receiver="instance-a", service="payments",
                                             environment_id="a" * 32, session_id="fresh-task")
            result = tool()
            self.assertEqual(len(result["facts"]), 1)
            self.assertEqual(result["backend"], "openviking")
            self.assertTrue(result["historical_only"])
            self.backend.unavailable = True
            unavailable = tool()
            self.assertIn("error", unavailable)
            self.assertEqual(unavailable["facts"], [])
            before = self.backend.calls
            wrong = memory.configured_lookup(root=self.root, receiver="instance-b", service="payments",
                                              environment_id="a" * 32, session_id="other-task")()
            self.assertIn("PermissionError", wrong["error"])
            self.assertEqual(self.backend.calls, before)

    def test_runbooks_only_describe_the_current_assigned_method(self):
        self.store.revise(scope=self.scope, key="runbook:oncall-alert-triage",
            content={"sha256": "d" * 64, "body": "previous method"}, sources=self.sources)
        self.store.drain(self.backend)
        runtime = Path(self.config["runtime_root"])
        (runtime / "learning").mkdir()
        assignment = runtime / "learning/state.json"
        assignment.write_text(json.dumps({"active_sha256": "e" * 64}))
        with patch.dict(os.environ, {"ONCALL_MEMORY_CONFIG": str(self.bound)}), patch.object(memory, "provider", return_value=self.backend):
            tool = memory.configured_lookup(root=self.root, receiver="instance-a", service="payments",
                                             environment_id="a" * 32, session_id="fresh-task")
            self.assertEqual(tool()["runbooks"], [])
            assignment.write_text(json.dumps({"active_sha256": "d" * 64}))
            self.assertEqual(len(tool()["runbooks"]), 1)

    def test_source_receipt_hashes_actual_original_session_item_bytes(self):
        worker = self.root / "worker"
        (worker / "hermes").mkdir(parents=True)
        with sqlite3.connect(worker / "hermes/state.db") as db:
            db.execute("CREATE TABLE messages(id INTEGER,session_id TEXT,content TEXT)")
            db.execute("INSERT INTO messages VALUES(5,'s','现场回执\\n')")
        source = memory.source_item(worker, "s", 5)
        self.assertEqual(source["sha256"], memory.digest("现场回执\\n"))
        with self.assertRaises(ValueError):
            memory.source_item(worker, "s", 6)


if __name__ == "__main__":
    unittest.main()

"""Knowledge-tool boundaries; native evidence uses a labeled adapter fixture."""

from contextlib import closing
import ast
import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall.paths import prepare
prepare()
from apps.oncall.knowledge_tools import lookup
from hermes_runtime import SOURCE, ONCALL_SKILL, PATROL_SKILL, open_sessions, install_oncall_skill
from patrol import connect, inspect_service
from fixture import respond
from local_model import serve


class KnowledgeToolTests(unittest.TestCase):
    def test_provider_failure_is_explicit_and_cannot_return_sqlite_facts(self):
        module = SimpleNamespace(configured_lookup=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("private details")))
        with patch.dict(sys.modules, {"apps.oncall.memory": module}):
            result = lookup(root=Path("/tmp/state"), receiver="instance-a", service="payments",
                            environment_id="a" * 32, session_id="session")
        self.assertEqual(result["backend"], "openviking")
        self.assertTrue(result["historical_only"])
        self.assertFalse(result["success"])
        self.assertIn("error", result)
        self.assertNotIn("private details", json.dumps(result))
        self.assertNotIn("facts", result)

    def test_lookup_before_fresh_scope_does_not_contact_provider(self):
        reader = unittest.mock.Mock()
        with patch.dict(sys.modules, {"apps.oncall.memory": SimpleNamespace(configured_lookup=reader)}):
            result = lookup(root=Path("/tmp/state"), receiver="instance-a", service="payments",
                            environment_id=None, session_id="session")
        reader.assert_not_called()
        self.assertIn("Read current service metrics", result["error"])
        self.assertFalse(result["success"])

    def test_successful_retrieval_of_historical_errors_is_not_a_tool_failure(self):
        from apps.oncall.integrations.skillclaw_compat import classify_tool_result
        upstream = ROOT / ".deps/SkillClaw/skillclaw/api_server.py"
        if not upstream.is_file():
            self.skipTest("pinned SkillClaw classifier required")
        # Execute only the pinned pure classifier and its pattern table. The
        # server imports/configuration and any remote model remain inactive.
        tree = ast.parse(upstream.read_text())
        nodes = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_classify_tool_error"
                 or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                 and node.target.id == "_ERROR_PATTERNS"]
        namespace = {"re": re}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(upstream), "exec"), namespace)
        original = namespace["_classify_tool_error"]

        receipt = {"backend": "openviking", "historical_only": True,
                   "facts": [{"content": {"error_rate": 0.15, "analysis": "Historical TimeoutError"}}]}
        self.assertEqual(classify_tool_result(json.dumps(receipt), original), (True, "timeout"))
        module = SimpleNamespace(configured_lookup=lambda **kwargs: lambda: receipt)
        with patch.dict(sys.modules, {"apps.oncall.memory": module}):
            result = lookup(root=Path("/tmp/state"), receiver="instance-a", service="payments",
                            environment_id="a" * 32, session_id="session")
        self.assertTrue(result["success"])
        self.assertEqual(classify_tool_result(json.dumps(result), original), (False, None))
        self.assertEqual(result["facts"], receipt["facts"])
        module = SimpleNamespace(configured_lookup=lambda **kwargs: lambda: {"error": "Provider unavailable"})
        with patch.dict(sys.modules, {"apps.oncall.memory": module}):
            result = lookup(root=Path("/tmp/state"), receiver="instance-a", service="payments",
                            environment_id="a" * 32, session_id="session")
        self.assertEqual(classify_tool_result(json.dumps(result), original), (True, "structured_failure"))

    @unittest.skipUnless(importlib.util.find_spec("openai") is not None and (SOURCE / "run_agent.py").exists(),
                         "pinned native Hermes runtime required")
    def test_actual_hermes_patrol_persists_scoped_knowledge_receipt_and_uses_fresh_health(self):
        if os.environ.get("RSI_MEMORY_TOOL_TEST_CHILD") != "1":
            result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                "KnowledgeToolTests.test_actual_hermes_patrol_persists_scoped_knowledge_receipt_and_uses_fresh_health"],
                env={**os.environ, "RSI_MEMORY_TOOL_TEST_CHILD": "1"}, capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return
        # This is a real Hermes/HTTP-model/tool loop. The knowledge backend is a
        # fixture here, not evidence of native OpenViking search quality.
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "instance-a"
            state.mkdir()
            scope = {"tenant": "fixture-team", "environment": "a" * 32, "service": "payments"}
            receipt = {"backend": "openviking", "historical_only": True, "scope": scope,
                       "facts": [{"content": {"old_status": "abnormal"}, "revision_id": "fixture-revision",
                                  "sources": [{"session_id": "prior", "item_id": 7, "sha256": "b" * 64}]}],
                       "runbooks": []}
            requests = []

            def configured(**kwargs):
                requests.append(kwargs)
                if len(requests) > 1:
                    raise RuntimeError("provider fixture unavailable")
                return lambda: receipt

            snapshot = {"source": "live_lab_http_requests", "service": "payments", "environment_id": "a" * 32,
                        "window_start": 100, "window_end": 130, "requests": 40, "error_rate": 0.0, "p95_ms": 30}
            with patch.dict(sys.modules, {"apps.oncall.memory": SimpleNamespace(configured_lookup=configured)}), \
                 patch.dict("os.environ", {"ONCALL_MEMORY_CONFIG": "/operator/fixture.json"}), \
                 closing(connect(state)) as db, open_sessions(state, fresh=True) as sessions, serve(respond) as url:
                skill = install_oncall_skill(state)
                patrol_skill = install_oncall_skill(state, name=PATROL_SKILL)
                record = inspect_service(sessions, db, "payments", state,
                    {"base_url": url, "api_key": "fixture", "model": "fixture"}, skill,
                    patrol_skill_sha=patrol_skill, snapshot=lambda *_args: snapshot)
                rows = sessions.get_messages(record["session_id"])
                calls = {call["id"]: call["function"]["name"] for row in rows for call in row.get("tool_calls") or []}
                memory = [row for row in rows if row["role"] == "tool" and calls.get(row["tool_call_id"]) == "knowledge_lookup"]
                self.assertEqual(len(memory), 1)
                self.assertEqual(json.loads(memory[0]["content"]), {**receipt, "success": True})
                self.assertEqual(record["status"], "healthy")
                self.assertLess(record["evidence_item_id"], memory[0]["id"])
                self.assertLess(memory[0]["id"], record["report_item_id"])
                boundary = record["report_item_id"]
                record = inspect_service(sessions, db, "payments", state,
                    {"base_url": url, "api_key": "fixture", "model": "fixture"}, skill,
                    patrol_skill_sha=patrol_skill, snapshot=lambda *_args: {**snapshot, "window_start": 140, "window_end": 170})
                rows = [row for row in sessions.get_messages(record["session_id"]) if row["id"] > boundary]
                calls = {call["id"]: call["function"]["name"] for row in rows for call in row.get("tool_calls") or []}
                errors = [json.loads(row["content"]) for row in rows if row["role"] == "tool"
                          and calls.get(row["tool_call_id"]) == "knowledge_lookup"]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["backend"], "openviking")
                self.assertIn("error", errors[0])
                self.assertNotIn("facts", errors[0])
                self.assertEqual(record["status"], "healthy")
            self.assertEqual(requests[0]["environment_id"], "a" * 32)
            self.assertEqual(requests[0]["receiver"], "instance-a")
            self.assertEqual(requests[0]["service"], "payments")
            import gc
            import hermes_logging
            gc.collect()
            hermes_logging._reset_queued_handlers()


if __name__ == "__main__":
    unittest.main()

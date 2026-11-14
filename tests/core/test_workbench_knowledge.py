"""Workbench memory management reflects delivered heads, not cache retrieval."""
import json
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall.paths import prepare
prepare()
from workbench import Workbench
from workbench import file_hash
from workbench_memory import openviking_state
from self_evolving_harness.knowledge import KnowledgeStore, content_digest


class CitationFixture:
    def put_revision(self, record):
        return {"uri": "viking://fixture/" + record["revision_id"], "revision_id": record["revision_id"],
                "content_sha256": content_digest(record)}


class WorkbenchKnowledgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.template = self.root / "operator.json"
        self.template.write_text('{"schema_version":1,"backend":"openviking"}')
        self.workbench = Workbench(self.root / "ui", ROOT / "apps/oncall/workflow.py", mode="system",
                                   memory_config=self.template)
        self.state = self.root / "memory"
        self.state.mkdir()
        self.binding = self.root / "binding.json"
        self.binding.write_text(json.dumps({"backend": "openviking", "namespace": "fixture-space", "tenant": "team",
            "state_dir": str(self.state), "instances": {"instance-a": "a" * 32, "instance-b": "b" * 32},
            "services": ["payments"]}))

    def bind_active(self):
        self.workbench.active_run = self.workbench.root / "runs/current"
        self.workbench.active_run.mkdir(parents=True)
        (self.workbench.active_run / "service.json").write_text(json.dumps({"memory_binding": str(self.binding)}))

    def test_start_agent_passes_operator_template_and_children_get_actual_binding(self):
        with patch.object(self.workbench, "acknowledge_recovered_alerts", return_value={"acknowledged": [], "errors": []}), \
             patch.object(self.workbench, "spawn") as spawn:
            self.workbench.start_agent()
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[argv.index("--memory-config") + 1], str(self.template))
        self.workbench.active_run = None
        self.bind_active()
        with patch("workbench.subprocess.Popen") as popen:
            self.workbench.spawn(["python", "chat.py"], self.root / "log")
            self.assertEqual(popen.call_args.kwargs["env"]["ONCALL_MEMORY_CONFIG"], str(self.binding))

    def test_system_workbench_reopens_the_same_environment_scope(self):
        with patch.object(self.workbench, "spawn") as spawn, \
             patch("workbench.get_snapshot", return_value={"source": "live_lab_http_requests"}):
            self.workbench.__enter__()
        self.assertEqual(spawn.call_count, 2)
        self.assertTrue(all("--resume" in call.args[0] for call in spawn.call_args_list))

    def test_same_runtime_resumes_and_memory_enable_is_an_explicit_migration(self):
        self.bind_active()
        run = self.workbench.active_run
        saved = {"mode": "local-script-model", "model": "patrol-fixture", "model_endpoint": "local",
                 "implementation": str(self.workbench.implementation_file),
                 "implementation_sha256": file_hash(self.workbench.implementation_file)}
        (run / "service-binding.json").write_text(json.dumps({"configuration": saved}))
        with patch.object(self.workbench, "acknowledge_recovered_alerts", return_value={"acknowledged": [], "errors": []}), \
             patch.object(self.workbench, "spawn") as spawn:
            result = self.workbench.start_agent()
        self.assertTrue(result["resumed"])
        self.assertEqual(self.workbench.active_run, run)
        self.assertIn("--resume", spawn.call_args.args[0])
        self.assertIn("--enable-memory", spawn.call_args.args[0])

    def test_changed_implementation_creates_a_new_run_and_preserves_old_evidence(self):
        self.bind_active()
        run = self.workbench.active_run
        (run / "service-binding.json").write_text(json.dumps({"configuration": {
            "mode": "local-script-model", "model": "patrol-fixture", "model_endpoint": "local",
            "implementation": str(self.workbench.implementation_file), "implementation_sha256": "0" * 64}}))
        marker = run / "prior-evidence.json"
        marker.write_text('{"prior":true}')
        with patch.object(self.workbench, "acknowledge_recovered_alerts", return_value={"acknowledged": [], "errors": []}), \
             patch.object(self.workbench, "spawn") as spawn:
            result = self.workbench.start_agent()
        self.assertFalse(result["resumed"])
        self.assertNotEqual(self.workbench.active_run, run)
        self.assertNotIn("--resume", spawn.call_args.args[0])
        self.assertEqual(marker.read_text(), '{"prior":true}')

    @unittest.skipUnless(importlib.util.find_spec("openai") is not None,
                         "pinned native runtime required")
    def test_actual_http_restart_preserves_scope_native_sessions_and_service_evidence(self):
        script = r'''
import sys,time,json,sqlite3,hashlib
from pathlib import Path
root=Path(sys.argv[1]);repo=Path(sys.argv[2]);sys.path.insert(0,str(repo))
from apps.oncall.paths import prepare
prepare()
from workbench import Workbench
from environment import get_snapshot
from apps.oncall import service
def counts(run):
 values={}
 for a in ('instance-a','instance-b'):
  with sqlite3.connect(run/'patrols'/a/'patrol.sqlite') as db:values[a]=db.execute('SELECT count(*) FROM inspections').fetchone()[0]
 return values
def wait_growth(run,before):
 deadline=time.monotonic()+20
 while not all(counts(run)[a]>before[a] for a in before):
  if time.monotonic()>deadline:raise AssertionError('Resumed patrol did not advance')
  time.sleep(.1)
implementation=repo/'apps/oncall/workflow.py'
with Workbench(root,implementation,live=False,mode='system') as ui:
 identities={a:get_snapshot(root/'environments'/a,'payments')['environment_id'] for a in ('instance-a','instance-b')}
 ui.start_agent();run=ui.active_run
 deadline=time.monotonic()+20
 while True:
  ready=[]
  for a in identities:
   p=run/'patrols'/a/'patrol.sqlite'
   try:
    with sqlite3.connect(p) as db: ready.append(db.execute('SELECT count(*) FROM inspections').fetchone()[0]>0)
   except sqlite3.Error:ready.append(False)
  if all(ready):break
  if time.monotonic()>deadline:raise AssertionError('Native patrol did not record both instances')
  time.sleep(.1)
 ui.stop(ui.agent)
 approved=root/'preapproved.md';approved.write_bytes(service.SEED.read_bytes()+b'\nPreapproved workbench restart fixture.\n')
 digest=service.sha256(approved);service.publish(run,approved,digest)
 status=service.load_json(run/'learning/state.json');status['active_sha256']=digest;service.write_json(run/'learning/state.json',status)
 before_counts=counts(run)
 result=ui.start_agent();assert result['resumed'] is True and ui.active_run==run
 wait_growth(run,before_counts)
files=[run/'service.json',*[run/'patrols'/a/'hermes/state.db' for a in identities]]
before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
with Workbench(root,implementation,live=False,mode='system') as reopened:
 assert {a:get_snapshot(root/'environments'/a,'payments')['environment_id'] for a in identities}==identities
 assert {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}==before
 assert reopened.active_run==run
 sessions={}
 for a in identities:
  with sqlite3.connect(run/'patrols'/a/'hermes/state.db') as db:sessions[a]={row[0] for row in db.execute('SELECT id FROM sessions')}
 before_counts=counts(run)
 result=reopened.start_agent();assert result['resumed'] is True and reopened.active_run==run
 wait_growth(run,before_counts)
 assert service.load_json(run/'learning/state.json')['active_sha256']==digest
 for a in identities:
  assert service.sha256(run/'receivers'/a/service.ONCALL_SKILL/'SKILL.md')==digest
  with sqlite3.connect(run/'patrols'/a/'hermes/state.db') as db:assert sessions[a]<={row[0] for row in db.execute('SELECT id FROM sessions')}
print('PASS actual HTTP scope and existing native SessionDB/service evidence preserved')
'''
        result = subprocess.run([sys.executable, "-B", "-c", script,
            str(self.root / "native-ui"), str(ROOT)], capture_output=True, text=True, timeout=70)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_openviking_mode_cannot_dispatch_old_sqlite_memory_jobs(self):
        self.assertEqual(self.workbench.memory_snapshot()["status"], "waiting_service")
        with self.assertRaisesRegex(ValueError, "自动复盘"):
            self.workbench.start_memory("review", None)
        with self.assertRaisesRegex(ValueError, "自动复盘"):
            self.workbench.start_memory("recall", "incident")
        with patch("workbench.subprocess.Popen") as popen, self.assertRaisesRegex(ValueError, "尚未就绪"):
            self.workbench.spawn(["python", "workbench_chat.py"], self.root / "chat.log")
        popen.assert_not_called()

    def test_management_view_excludes_pending_expired_and_superseded_facts(self):
        store = KnowledgeStore(self.state / "knowledge.sqlite3")
        scope = {"tenant": "team", "environment": "a" * 32, "service": "payments"}
        source = [{"session_id": "actual-source", "item_id": 4, "sha256": "c" * 64}]
        record = store.revise(scope=scope, key="incident:one", content={"incident_id": "one", "service": "payments"}, sources=source)
        self.assertEqual(openviking_state(self.binding)["facts"], [])
        self.assertEqual(openviking_state(self.binding)["pending_publications"], 1)
        store.drain(CitationFixture())
        facts = openviking_state(self.binding)["facts"]
        self.assertEqual(facts[0]["revision_id"], record["revision_id"])
        self.assertEqual(facts[0]["publication_status"], "delivered")
        store.revise(scope=scope, key="incident:one", content={"incident_id": "one", "service": "payments", "fixed": True},
                     sources=source, expected_revision=record["revision_id"])
        self.assertEqual(openviking_state(self.binding)["facts"], [])
        store.revise(scope=scope, key="incident:expired", content={"service": "payments"}, sources=source,
                     valid_until=time.time() - 1)
        store.drain(CitationFixture())
        self.assertEqual(len(openviking_state(self.binding)["facts"]), 1)

    def test_missing_bound_file_is_explicit_error_not_sqlite_fallback(self):
        self.bind_active()
        self.binding.unlink()
        memory = self.workbench.memory_snapshot()
        self.assertEqual(memory["backend"], "openviking")
        self.assertEqual(memory["status"], "error")
        self.assertEqual(memory["facts"], [])

    def test_management_runbooks_follow_current_approved_assignment(self):
        config = json.loads(self.binding.read_text())
        config["runtime_root"] = str(self.root / "run")
        self.binding.write_text(json.dumps(config))
        assignment = Path(config["runtime_root"]) / "learning/state.json"
        assignment.parent.mkdir(parents=True)
        assignment.write_text(json.dumps({"active_sha256": "d" * 64}))
        store = KnowledgeStore(self.state / "knowledge.sqlite3")
        scope = {"tenant": "team", "environment": "a" * 32, "service": "payments"}
        sources = [{"session_id": "actual-source", "item_id": 4, "sha256": "c" * 64}]
        record = store.revise(scope=scope, key="runbook:oncall-alert-triage",
            content={"sha256": "d" * 64, "body": "Prior approved method"}, sources=sources)
        store.revise(scope=scope, key="incident:one", content={"incident_id": "one"}, sources=sources)
        store.drain(CitationFixture())
        self.assertEqual(openviking_state(self.binding)["runbooks"][0]["revision_id"], record["revision_id"])
        assignment.write_text(json.dumps({"active_sha256": "e" * 64}))
        self.assertEqual(openviking_state(self.binding)["runbooks"], [])
        assignment.unlink()
        snapshot = openviking_state(self.binding)
        self.assertEqual(snapshot["runbooks"], [])
        self.assertEqual(snapshot["runbook_status"], "assignment_unavailable")
        self.assertEqual(snapshot["status"], "degraded")
        self.assertEqual(len(snapshot["facts"]), 1)

    def test_workbench_knowledge_aggregation_preserves_structured_read_status(self):
        from apps.oncall import workbench_chat
        fixture = {"backend": "openviking", "historical_only": True, "success": True,
                   "facts": [{"content": {"error_rate": 0.2, "summary": "Historical TimeoutError"}}]}
        with patch("environment.get_snapshot", return_value={"environment_id": "a" * 32}), \
             patch("apps.oncall.knowledge_tools.lookup", return_value=fixture):
            result = workbench_chat.knowledge_context(self.root,
                {"instance": "instance-a", "service": "payments"}, "session")
        self.assertTrue(result["success"])
        with patch("environment.get_snapshot", side_effect=RuntimeError("not ready")):
            result = workbench_chat.knowledge_context(self.root,
                {"instance": "instance-a", "service": "payments"}, "session")
        self.assertFalse(result["success"])
        self.assertIn("error", result)
        self.assertEqual(result["scopes"][0]["knowledge"]["success"], False)


if __name__ == "__main__":
    unittest.main()

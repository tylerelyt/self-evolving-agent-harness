"""Real gateway -> fixed tool subprocess -> live HTTP fault recovery."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall.paths import prepare
prepare()
from environment import classify, get_snapshot, inject_fault, load_json, write_json
from incident_runtime import IncidentRuntime
from workbench import Workbench
from apps.oncall.enterprise import EnterpriseRecovery
from apps.dedicated.__main__ import ToolGateway, make_server


class EnterpriseRecoveryTests(unittest.TestCase):
    def test_unknown_gateway_outcome_is_not_dispatched_again(self):
        binding = EnterpriseRecovery.__new__(EnterpriseRecovery)
        binding.engineer = Mock()
        binding.worker = Mock()
        binding.approve = Mock(return_value={"operation": "uncertain", "spec": {}, "approval": {}})
        binding.engineer.reconcile.return_value = {"state": "unknown"}
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            binding.execute({"operation": "uncertain"}, Path("unused"))
        binding.worker.dispatch.assert_not_called()

    def test_gateway_failure_is_not_converted_to_local_execution(self):
        binding = EnterpriseRecovery.__new__(EnterpriseRecovery)
        binding.engineer = Mock()
        binding.worker = Mock()
        binding.approve = Mock(return_value={"operation": "uncertain", "spec": {}, "approval": {}})
        binding.engineer.reconcile.side_effect = HTTPError("http://127.0.0.1", 500, "unavailable", {}, None)
        with self.assertRaises(HTTPError):
            binding.execute({"operation": "uncertain"}, Path("unused"))
        binding.worker.dispatch.assert_not_called()

    def test_workbench_approval_clears_actual_fault_via_gateway_and_rejects_scope_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "environments/instance-a"
            state.mkdir(parents=True)
            log = (root / "environment.log").open("w")
            child = subprocess.Popen([sys.executable, "-B", str(ROOT / "apps/oncall/patrol/environment.py"),
                                      "--state", str(state), "serve"], stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        observed = get_snapshot(state, "payments")
                        if observed["requests"] >= 10:
                            break
                    except (OSError, ValueError, KeyError):
                        pass
                    if time.monotonic() > deadline:
                        self.fail("HTTP environment did not start")
                    time.sleep(.1)
                inject_fault(state, "payments", "on")
                while classify(get_snapshot(state, "payments")) != "abnormal":
                    if time.monotonic() > deadline:
                        self.fail("Injected fault did not affect actual HTTP metrics")
                    time.sleep(.1)
                environment_id = observed["environment_id"]
                tool_config = root / "tool.json"
                write_json(tool_config, {"receipts": "tool-receipts", "environments": [{"tenant": "acme",
                    "environment_id": environment_id, "instance": "instance-a", "state": str(state), "services": ["payments"]}]})
                command = [sys.executable, str(ROOT / "apps/oncall/recovery_tool.py")]
                config = {"schema_version": 1, "signing_key_env": "TEST_GATEWAY_KEY", "journal": "actions.sqlite3",
                    "principals": [{"name": "engineer", "token_env": "TEST_ENGINEER", "roles": ["approve"],
                                    "grants": [["acme", environment_id, "clear_lab_fault"]]},
                                   {"name": "harness", "token_env": "TEST_HARNESS", "roles": ["execute"],
                                    "grants": [["acme", environment_id, "clear_lab_fault"]]}],
                    "tools": {"clear_lab_fault": {"apply": [*command, "apply", "--config", str(tool_config)],
                                                    "observe": [*command, "observe", "--config", str(tool_config)]}}}
                write_json(root / "gateway.json", config)
                private_env = {"TEST_GATEWAY_KEY": "k" * 32, "TEST_ENGINEER": "e" * 32, "TEST_HARNESS": "h" * 32}
                with patch.dict(os.environ, private_env):
                    gateway = ToolGateway(root / "gateway.json")
                server = make_server(gateway)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    binding = {"tenant": "acme", "executor": "harness", "gateway_url": f"http://127.0.0.1:{server.server_port}",
                               "approver_token_env": "TEST_ENGINEER", "executor_token_env": "TEST_HARNESS"}
                    write_json(root / "binding.json", binding)
                    with patch.dict(os.environ, {**private_env, "ONCALL_ACTION_CONFIG": str(root / "binding.json")}):
                        workbench = Workbench(root, ROOT / "apps/oncall/workflow.py")
                    with patch.dict(os.environ, {**private_env, "ONCALL_ACTION_CONFIG": str(root / "binding.json")}):
                        child_env = workbench.enterprise_recovery.child_environment()
                    self.assertNotIn("TEST_ENGINEER", child_env)
                    self.assertNotIn("TEST_HARNESS", child_env)
                    self.assertNotIn("ONCALL_ACTION_CONFIG", child_env)
                    flag = state / "payments.fault"
                    marker = flag.stat()
                    incident = "lab-payments-0123456789ab"
                    directory = root / "recovery" / incident
                    directory.mkdir(parents=True)
                    request = {"source": "run-1/instance-a", "instance": "instance-a", "service": "payments",
                        "environment_id": environment_id, "incident_id": incident, "operation": incident + "-clear-fault",
                        "scope": f"instance-a/{environment_id}/payments", "session_id": "native-source-fixture",
                        "report_item_id": 12, "report_sha256": "a" * 64, "page_sha256": "b" * 64,
                        "skill_sha256": "c" * 64, "evidence": "investigation.json",
                        "fault_generation": {"inode": marker.st_ino, "mtime_ns": marker.st_mtime_ns},
                        "approval": None, "execution": None}
                    runtime = IncidentRuntime(root / "recovery/incidents.sqlite3")
                    runtime.observe(incident + "-trigger", incident, request["scope"], 10, 10,
                                    source="fixture", quality="unhealthy", evidence={})
                    runtime.start(incident, request["skill_sha256"])
                    runtime.request_action(incident, request["operation"], request["evidence"], action="clear_lab_fault")
                    def snapshot(_incident):
                        result = dict(request)
                        for name in ("approval", "execution"):
                            path = directory / (name + ".json")
                            result[name] = load_json(path) if path.exists() else None
                        return result
                    body = {key: request[key] for key in ("source", "incident_id", "operation", "scope", "environment_id")}
                    body["confirmed"] = True
                    # Source evidence verification is already covered by native Session fixtures;
                    # here the real HTTP fault, gateway, authorization, and tool process are exercised.
                    with patch.object(workbench, "recovery_snapshot", side_effect=snapshot), \
                         patch.object(workbench, "recovery_evidence", return_value=request):
                        result = workbench.approve_recovery(body)
                        again = workbench.approve_recovery(body)
                    self.assertFalse(flag.exists())
                    self.assertEqual(result["approval"]["source"], "dedicated_action_gateway")
                    self.assertEqual(result["approval"]["actor"], "engineer")
                    self.assertEqual(result["execution"], again["execution"])
                    self.assertEqual(result["execution"]["effect"]["authority"], "dedicated_action_gateway")
                    self.assertEqual(gateway.journal.get(request["operation"])["state"], "completed")
                    self.assertEqual(len(list((root / "tool-receipts").glob("*.json"))), 1)
                    escaped = {**workbench.enterprise_recovery.spec(request), "environment": "production"}
                    with self.assertRaises(HTTPError) as failure:
                        workbench.enterprise_recovery.engineer.approve("harness", escaped)
                    self.assertEqual(failure.exception.code, 403)
                    # A granted action scope still cannot select another service or state path.
                    changed = workbench.enterprise_recovery.spec(request)
                    changed["parameters"] = {**changed["parameters"], "service": "inventory"}
                    approval = workbench.enterprise_recovery.engineer.approve("harness", changed)
                    with self.assertRaises(HTTPError):
                        workbench.enterprise_recovery.worker.dispatch("escape-service", changed, approval)
                    self.assertEqual(gateway.journal.get("escape-service")["state"], "unknown")
                    self.assertEqual(len(list((root / "tool-receipts").glob("*.json"))), 1)
                    inject_fault(state, "payments", "on")
                    stale = workbench.enterprise_recovery.spec(request)
                    stale_approval = workbench.enterprise_recovery.engineer.approve("harness", stale)
                    with self.assertRaises(HTTPError):
                        workbench.enterprise_recovery.worker.dispatch("wrong-generation", stale, stale_approval)
                    self.assertTrue(flag.exists())
                    self.assertEqual(gateway.journal.get("wrong-generation")["state"], "unknown")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
            finally:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                log.close()


if __name__ == "__main__":
    unittest.main()

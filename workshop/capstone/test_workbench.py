"""Regression tests for workbench evidence and read-only session inspection."""

from copy import deepcopy
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from workbench import Workbench, file_hash, make_server


IMPLEMENTATION = '''
def patrol_config():
    return {"services": ["payments"], "sample": 1, "interval": 1}

def route_alert(steer, alert):
    return True

def branch_report(sessions, record):
    return "child", {}

def notify_operator(path, recipient, scope, result, evidence):
    return path

def recall_memory(state, memory, environment_state, scope, options):
    return {}

def release_policy(results, baseline_sha, candidate_sha):
    return True
'''


class WorkbenchTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.implementation = self.directory / "self_improvement_agent.py"
        self.implementation.write_text(IMPLEMENTATION, encoding="utf-8")
        self.workbench = Workbench(self.directory / "workbench", self.implementation)
        self.addCleanup(self.workbench.close)

    def snapshot(self):
        metrics = {"source": "live_lab_http_requests", "environment_id": "test-env",
                   "window_start": 100, "window_end": 130, "requests": 80,
                   "error_rate": 0, "p95_ms": 12}
        with patch("workbench.get_snapshot", return_value=metrics):
            return self.workbench.snapshot()


class CheckEvidenceTests(WorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.check_directory = self.workbench.root / "checks" / "external-cli-check"
        self.check_directory.mkdir(parents=True)
        checks = []
        for number in range(1, 4):
            filename = f"stage-{number}.json"
            (self.check_directory / filename).write_text("{}", encoding="utf-8")
            checks.append({"stage": number, "status": "passed", "evidence": [filename]})
        self.evidence = {
            "schema": "oncall-workshop-check-v1", "status": "passed", "stage": 3,
            "implementation": {"path": str(self.implementation),
                               "sha256": file_hash(self.implementation)},
            "checks": checks, "completed_at": 140,
        }

    def write_check(self, evidence=None):
        content = self.evidence if evidence is None else evidence
        (self.check_directory / "check.json").write_text(json.dumps(content), encoding="utf-8")

    def test_external_cli_check_is_discovered_without_a_registered_job(self):
        self.write_check()
        self.assertEqual(self.workbench.record["checks"], [])

        status = self.snapshot()

        self.assertEqual(len(status["checks"]), 1)
        check = status["checks"][0]
        self.assertEqual(check["id"], "external-cli-check")
        self.assertTrue(check["evidence_valid"])
        self.assertTrue(check["verified"])
        self.assertEqual(check["implementation_sha256"], file_hash(self.implementation))
        self.assertEqual([stage["verified"] for stage in status["agent"]["stages"]],
                         [True, True, True, False])

    def test_passed_metadata_without_all_stage_evidence_is_not_verified(self):
        variants = []
        metadata_only = deepcopy(self.evidence)
        metadata_only.pop("checks")
        variants.append(metadata_only)
        missing_stage = deepcopy(self.evidence)
        missing_stage["checks"].pop(1)
        variants.append(missing_stage)
        empty_evidence = deepcopy(self.evidence)
        empty_evidence["checks"][1]["evidence"] = []
        variants.append(empty_evidence)
        failed_stage = deepcopy(self.evidence)
        failed_stage["checks"][1]["status"] = "failed"
        variants.append(failed_stage)

        for evidence in variants:
            with self.subTest(evidence=evidence):
                self.write_check(evidence)
                status = self.snapshot()
                self.assertFalse(status["checks"][0]["evidence_valid"])
                self.assertFalse(status["checks"][0]["verified"])
                self.assertFalse(any(stage["verified"] for stage in status["agent"]["stages"]))

    def test_deleting_an_evidence_file_revokes_verification(self):
        self.write_check()
        self.assertTrue(self.snapshot()["checks"][0]["verified"])

        (self.check_directory / "stage-2.json").unlink()

        check = self.snapshot()["checks"][0]
        self.assertFalse(check["evidence_valid"])
        self.assertFalse(check["verified"])

    def test_invalid_schema_or_failed_check_cannot_verify_stages(self):
        for field, value in (("schema", "unrelated-check"), ("status", "failed")):
            with self.subTest(field=field):
                evidence = deepcopy(self.evidence)
                evidence[field] = value
                self.write_check(evidence)
                self.assertFalse(self.snapshot()["checks"][0]["verified"])

    def test_complete_evidence_for_other_code_does_not_verify_current_code(self):
        self.evidence["implementation"]["sha256"] = "0" * 64
        self.write_check()

        status = self.snapshot()

        self.assertTrue(status["checks"][0]["evidence_valid"])
        self.assertFalse(status["checks"][0]["verified"])
        self.assertFalse(any(stage["verified"] for stage in status["agent"]["stages"]))

    def test_editing_implementation_revokes_previous_verification(self):
        self.write_check()
        self.assertTrue(self.snapshot()["checks"][0]["verified"])

        self.implementation.write_text(IMPLEMENTATION + "\n# Revised implementation\n", encoding="utf-8")

        status = self.snapshot()
        self.assertTrue(status["checks"][0]["evidence_valid"])
        self.assertFalse(status["checks"][0]["verified"])
        self.assertFalse(any(stage["verified"] for stage in status["agent"]["stages"]))


class SessionTestCase(WorkbenchTestCase):
    def setUp(self):
        super().setUp()
        run = self.workbench.root / "runs" / "test-run"
        run.mkdir(parents=True)
        (run / "service.json").write_text("{}", encoding="utf-8")
        self.source = "test-run/instance-a"
        state = run / "patrols" / "instance-a"
        database = state / "hermes" / "state.db"
        database.parent.mkdir(parents=True)
        self.report_item = 600
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, parent_session_id TEXT,
                    started_at REAL, model TEXT, message_count INTEGER
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, tool_call_id TEXT, tool_name TEXT, tool_calls TEXT
                );
            """)
            connection.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", [
                ("patrol", None, 100, "local-test", 1200),
                ("other-session", None, 101, "local-test", 1),
            ])
            rows = [(number, "patrol", "assistant", f"message-{number}", None, None, "[]")
                    for number in range(1, 1201)]
            rows[self.report_item - 1] = (self.report_item, "patrol", "assistant", "saved incident report",
                                         None, None, "[]")
            rows.append((1201, "other-session", "assistant", "other incident", None, None, "[]"))
            connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


class SessionInspectionTests(SessionTestCase):
    def test_report_anchor_retains_report_after_hundreds_of_later_patrol_messages(self):
        result = self.workbench.session(self.source, "patrol", str(self.report_item))

        self.assertEqual(result["anchor_item_id"], self.report_item)
        self.assertEqual(result["messages"][-1]["id"], self.report_item)
        self.assertEqual(result["messages"][-1]["content"], "saved incident report")
        ids = [message["id"] for message in result["messages"]]
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(all(item <= self.report_item for item in ids))
        self.assertTrue(result["has_earlier_messages"])

    def test_unanchored_inspection_shows_latest_messages(self):
        result = self.workbench.session(self.source, "patrol")

        self.assertIsNone(result["anchor_item_id"])
        self.assertEqual(result["messages"][-1]["id"], 1200)
        self.assertTrue(result["has_earlier_messages"])

    def test_unknown_source_or_session_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "source"):
            self.workbench.session("unlisted/instance-a", "patrol", self.report_item)
        with self.assertRaisesRegex(ValueError, "session"):
            self.workbench.session(self.source, "unknown-session", self.report_item)

    def test_item_from_another_session_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Item"):
            self.workbench.session(self.source, "patrol", 1201)

    def test_missing_or_nonnumeric_item_is_rejected(self):
        for item in (99999, "not-an-item"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.workbench.session(self.source, "patrol", item)


class WorkbenchHTTPTests(SessionTestCase):
    def setUp(self):
        super().setUp()
        html = self.directory / "index.html"
        html.write_text("<p>Workbench test</p>", encoding="utf-8")
        self.server = make_server(self.workbench, "127.0.0.1", 0, html)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.shutdown)
        self.authority = f"127.0.0.1:{self.server.server_port}"

    def request(self, method, path, body=None, origin=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_cross_origin_action_is_rejected_before_dispatch(self):
        with patch.object(self.workbench, "action", return_value={"status": "accepted"}) as action:
            status, result = self.request("POST", "/api/actions", json.dumps({"action": "agent_stop"}),
                                          origin="https://other.example")
        self.assertEqual(status, 403)
        self.assertIn("error", result)
        action.assert_not_called()

    def test_same_origin_action_reaches_dispatch(self):
        body = {"action": "agent_stop"}
        with patch.object(self.workbench, "action", return_value={"status": "accepted"}) as action:
            status, result = self.request("POST", "/api/actions", json.dumps(body),
                                          origin=f"http://{self.authority}")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"status": "accepted"})
        action.assert_called_once_with(body)

    def test_approval_dispatch_retains_actual_http_context_without_claiming_an_identity(self):
        body = {"action": "recovery_approve", "confirmed": True}
        with patch.object(self.workbench, "action", return_value={"status": "verifying"}) as action:
            status, _ = self.request("POST", "/api/actions", json.dumps(body), origin=f"http://{self.authority}")
        self.assertEqual(status, 200)
        action.assert_called_once_with(body, request_context={"transport": "http", "origin": f"http://{self.authority}",
                                                             "host": self.authority})

    def test_session_endpoint_preserves_report_anchor(self):
        query = urlencode({"source": self.source, "session": "patrol", "item": self.report_item})
        status, result = self.request("GET", "/api/session?" + query)
        self.assertEqual(status, 200)
        self.assertEqual(result["anchor_item_id"], self.report_item)
        self.assertEqual(result["messages"][-1]["content"], "saved incident report")

    def test_session_endpoint_rejects_missing_source_and_foreign_item(self):
        invalid_queries = [{"session": "patrol", "item": self.report_item},
                           {"source": self.source, "session": "patrol", "item": 1201}]
        for parameters in invalid_queries:
            with self.subTest(parameters=parameters):
                status, result = self.request("GET", "/api/session?" + urlencode(parameters))
                self.assertEqual(status, 400)
                self.assertIn("error", result)

    def test_chat_endpoint_returns_the_same_new_conversation(self):
        with patch.object(self.workbench, "require_chat"):
            status, result = self.request("POST", "/api/actions", json.dumps({"action": "chat_new", "title": "A/B 调查"}))
        self.assertEqual(status, 200)
        metadata = result["conversation"]
        status, chat = self.request("GET", "/api/chat?" + urlencode({"conversation": metadata["id"]}))
        self.assertEqual(status, 200)
        self.assertEqual(chat["conversation"]["id"], metadata["id"])
        self.assertEqual(chat["conversation"]["title"], "A/B 调查")
        self.assertEqual(chat["messages"], [])
        self.assertEqual(chat["threads"], [])
        self.assertEqual(chat["jobs"], [])

    def test_chat_endpoint_rejects_a_foreign_or_missing_conversation(self):
        for identifier in ("", "../outside", "0" * 32):
            with self.subTest(identifier=identifier):
                status, result = self.request("GET", "/api/chat?" + urlencode({"conversation": identifier}))
                self.assertEqual(status, 400)
                self.assertIn("error", result)


class ChatControlTests(WorkbenchTestCase):
    def online(self):
        self.workbench.agent = Mock(returncode=None)
        self.workbench.agent.poll.return_value = None
        self.workbench.active_run = self.workbench.root / "runs" / "chat-test"
        self.workbench.active_run.mkdir(parents=True)
        (self.workbench.active_run / "service.json").write_text('{"status":"running"}')
        self.workbench.record["active_stage"] = 1

    def fake_process(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        return process

    def test_offline_agent_cannot_create_or_send_chat(self):
        self.assertFalse(self.workbench.chat_capability()["available"])
        with self.assertRaisesRegex(ValueError, "尚未上线"):
            self.workbench.action({"action": "chat_new"})
        with self.assertRaisesRegex(ValueError, "尚未上线"):
            self.workbench.action({"action": "chat_send", "conversation": "0" * 32, "question": "检查现场"})

    def test_missing_live_credentials_does_not_start_a_worker_or_fall_back(self):
        self.workbench.live = True
        with patch.dict(os.environ, {"GLM_API_KEY": ""}), patch.object(self.workbench, "spawn") as spawn:
            capability = self.workbench.chat_capability()
            self.assertFalse(capability["available"])
            self.assertEqual(capability["mode"], "live")
            self.assertIn("GLM_API_KEY", capability["reason"])
            with self.assertRaisesRegex(ValueError, "GLM_API_KEY"):
                self.workbench.start_agent()
            spawn.assert_not_called()

    def test_chat_rejects_path_traversal_and_invalid_context_before_spawning(self):
        self.online()
        conversation = self.workbench.new_conversation()["conversation"]
        with patch.object(self.workbench, "spawn") as spawn:
            for body in ({"conversation": "../outside"},
                         {"conversation": conversation["id"], "context": {"instance": "unknown"}},
                         {"conversation": conversation["id"], "context": {"untrusted": "text"}}):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    self.workbench.send_chat({"question": "检查当前现场", **body})
            spawn.assert_not_called()

    def test_each_conversation_has_an_independent_job_but_cannot_send_two_turns_at_once(self):
        self.online()
        first = self.workbench.new_conversation()["conversation"]
        second = self.workbench.new_conversation()["conversation"]
        with patch.object(self.workbench, "spawn", side_effect=[self.fake_process(), self.fake_process()]) as spawn:
            self.workbench.send_chat({"conversation": first["id"], "question": "检查 A"})
            self.workbench.send_chat({"conversation": second["id"], "question": "检查 B"})
            with self.assertRaisesRegex(ValueError, "正在回复"):
                self.workbench.send_chat({"conversation": first["id"], "question": "继续"})
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(len([job for job in self.workbench.record["jobs"] if job["status"] == "running"]), 2)
        for call in spawn.call_args_list:
            self.assertIn("--offline", call.args[0])

    def test_followup_uses_the_same_conversation_state_and_retains_the_original_title(self):
        self.online()
        conversation = self.workbench.new_conversation()["conversation"]
        state = self.workbench.root / "chat" / conversation["id"]
        first_process, second_process = self.fake_process(), self.fake_process()
        with patch.object(self.workbench, "spawn", side_effect=[first_process, second_process]) as spawn:
            self.workbench.send_chat({"conversation": conversation["id"], "question": "哪些服务异常？"})
            (state / "last-result.json").write_text('{"status":"completed"}')
            first_process.returncode = 0
            first_process.poll.return_value = 0
            self.workbench.refresh_jobs()
            self.workbench.send_chat({"conversation": conversation["id"], "question": "它的日志说明什么？"})
        paths = [call.args[0][call.args[0].index("--state") + 1] for call in spawn.call_args_list]
        self.assertEqual(paths, [str(state), str(state)])
        self.assertEqual(json.loads((state / "conversation.json").read_text())["title"], "哪些服务异常？")
        result = self.workbench.chat(conversation["id"])
        self.assertEqual(result["conversation"]["status"], "running")
        self.assertEqual([job["question"] for job in result["jobs"]], ["哪些服务异常？", "它的日志说明什么？"])

    def test_chat_failure_is_reported_without_marking_it_completed(self):
        self.online()
        conversation = self.workbench.new_conversation()["conversation"]
        process = self.fake_process()
        with patch.object(self.workbench, "spawn", return_value=process):
            self.workbench.send_chat({"conversation": conversation["id"], "question": "检查现场"})
        process.returncode = 1
        process.poll.return_value = 1
        state = self.workbench.root / "chat" / conversation["id"]
        (state / "last-result.json").write_text('{"status":"failed","error":"model request failed"}')
        result = self.workbench.chat(conversation["id"])
        self.assertEqual(result["conversation"]["status"], "failed")
        self.assertEqual(result["conversation"]["error"], "model request failed")

    def test_chat_sources_include_commander_and_real_investigation_threads(self):
        self.online()
        conversation = self.workbench.new_conversation()["conversation"]
        state = self.workbench.root / "chat" / conversation["id"]
        commander = state / "hermes" / "state.db"
        thread = state / "threads" / "thread-one" / "hermes" / "state.db"
        for database in (commander, thread):
            database.parent.mkdir(parents=True)
            database.touch()
        sources = self.workbench.sources()
        self.assertEqual(sources["chat:" + conversation["id"]], state)
        self.assertEqual(sources["chat-thread:" + conversation["id"] + "/thread-one"], thread.parent.parent)

    def test_existing_conversation_keeps_its_stage_and_version_when_new_code_is_applied(self):
        self.online()
        self.workbench.running_sha = "a" * 64
        original = self.workbench.new_conversation()["conversation"]
        self.workbench.record["active_stage"] = 4
        self.workbench.running_sha = "b" * 64
        learning = self.workbench.active_run / "learning" / "state.json"
        learning.parent.mkdir()
        learning.write_text(json.dumps({"active_sha256": "c" * 64}))
        current = self.workbench.new_conversation()["conversation"]
        old = self.workbench.chat(original["id"])["conversation"]
        self.assertEqual(old["active_stage"], 1)
        self.assertEqual(old["implementation_sha256"], "a" * 64)
        self.assertEqual(current["active_stage"], 4)
        self.assertEqual(current["implementation_sha256"], "b" * 64)
        self.assertEqual(current["skill_sha256"], "c" * 64)

    def test_model_configuration_exposes_only_its_file_path_and_model_name(self):
        secret = "credential-that-must-not-appear-in-the-api"
        self.workbench.live = True
        with patch.dict(os.environ, {"GLM_API_KEY": secret, "GLM_MODEL": "configured-model",
                                     "ONCALL_MODEL_ENV_FILE": "/tmp/model-connection.env"}):
            result = self.snapshot()
        self.assertEqual(result["chat"]["model"], "configured-model")
        self.assertEqual(result["agent"]["model_env_file"], "/tmp/model-connection.env")
        self.assertNotIn(secret, json.dumps(result))

    def test_conversation_list_keeps_version_pins_without_repeating_turns_or_runbooks(self):
        self.online()
        conversation = self.workbench.new_conversation()["conversation"]
        path = self.workbench.root / "chat" / conversation["id"] / "conversation.json"
        metadata = json.loads(path.read_text())
        metadata.update(turns=[{"question": "long prior turn"}], runbook_text="long immutable Skill content")
        path.write_text(json.dumps(metadata))
        listed = self.workbench.conversation_snapshots()[0]
        self.assertEqual(listed["active_stage"], conversation["active_stage"])
        self.assertEqual(listed["implementation_sha256"], conversation["implementation_sha256"])
        self.assertNotIn("turns", listed)
        self.assertNotIn("runbook_text", listed)
        self.assertEqual(self.workbench.chat(conversation["id"])["conversation"]["turns"], metadata["turns"])


class RecoveryApprovalTests(WorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.alias, self.service = "instance-a", "payments"
        self.incident = "lab-payments-0123456789ab"
        self.source = "recovery-run/instance-a"
        self.environment = self.workbench.root / "environments" / self.alias
        self.environment.mkdir(parents=True)
        self.flag = self.environment / "payments.fault"
        self.flag.write_text("intermittent")
        self.metrics = {"source": "live_lab_http_requests", "service": self.service, "environment_id": "recovery-environment",
                        "window_start": 100, "window_end": 130, "requests": 100, "error_rate": .5, "p95_ms": 20}
        self.evidence = {"source": self.source, "instance": self.alias, "service": self.service,
                         "environment_id": self.metrics["environment_id"], "incident_id": self.incident,
                         "scope": "instance-a/recovery-environment/payments", "report": {"analysis": "verified report"},
                         "session_id": "investigation-session", "report_item_id": 20, "skill_sha256": "a" * 64,
                         "observation": self.metrics, "evidence": "runs/recovery-run/investigation.json",
                         "report_sha256": "b" * 64, "page_sha256": "c" * 64}

    def request(self):
        with patch.object(self.workbench, "recovery_evidence", return_value=self.evidence):
            return self.workbench.action({"action": "recovery_request", "source": self.source, "incident_id": self.incident})

    def approve(self, request, **changes):
        body = {key: request[key] for key in ("source", "incident_id", "operation", "scope", "environment_id")}
        body.update(action="recovery_approve", confirmed=True)
        body.update(changes)
        with patch.object(self.workbench, "recovery_evidence", return_value=self.evidence), patch("fleet.get_snapshot", return_value=self.metrics):
            return self.workbench.action(body, request_context={"transport": "http", "origin": "http://127.0.0.1:8765"})

    def test_request_does_not_clear_fault_and_explicit_approval_executes_once(self):
        request = self.request()
        self.assertTrue(self.flag.is_file())
        self.assertEqual(request["runtime_state"], "AWAITING_APPROVAL")
        self.assertIsNone(request["approval"])
        result = self.approve(request)
        self.assertFalse(self.flag.exists())
        self.assertEqual(result["status"], "verifying")
        self.assertEqual(result["runtime_state"], "VERIFYING")
        self.assertFalse(result["approval"]["identity_verified"])
        self.assertEqual(result["approval"]["source"], "local_workbench_http")
        self.assertEqual(self.approve(request)["execution"], result["execution"])
        from incident_runtime import IncidentRuntime
        ledger = IncidentRuntime(self.workbench.root / "recovery/incidents.sqlite3").report()
        self.assertEqual(sum(row["kind"] == "action_executed" for row in ledger["events"]), 1)
        changes = [json.loads(line) for line in (self.environment / "changes.jsonl").read_text().splitlines()]
        self.assertEqual((changes[-1]["before"], changes[-1]["after"]), ("intermittent", "off"))

    def test_missing_confirmation_or_wrong_scope_cannot_execute(self):
        request = self.request()
        for changes in ({"confirmed": False}, {"scope": "instance-b/recovery-environment/payments"},
                        {"environment_id": "old-environment"}, {"operation": "another-operation"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.approve(request, **changes)
        self.assertTrue(self.flag.is_file())
        self.assertEqual(self.workbench.recovery_snapshot(self.incident)["runtime_state"], "AWAITING_APPROVAL")

    def test_reinjected_fault_is_not_cleared_by_an_older_approval(self):
        request = self.request()
        replacement = self.flag.with_suffix(".new")
        replacement.write_text("")
        replacement.replace(self.flag)
        with self.assertRaisesRegex(ValueError, "另一代故障"):
            self.approve(request)
        self.assertTrue(self.flag.is_file())
        self.assertIsNone(self.workbench.recovery_snapshot(self.incident)["execution"])

    def test_missing_report_and_old_environment_are_rejected_before_action_request(self):
        worker = self.workbench.root / "runs/recovery-run/patrols/instance-a"
        worker.mkdir(parents=True)
        from patrol import connect
        db = connect(worker)
        db.close()
        record = {"incident_id": self.incident, "observation": json.dumps(self.metrics), "recovered_inspection_id": None}
        for environment_id, error in (("old-environment", ValueError), (self.metrics["environment_id"], OSError)):
            with self.subTest(environment_id=environment_id), patch.object(self.workbench, "sources", return_value={self.source: worker}), \
                 patch("workbench.incidents", side_effect=lambda _db, service: [record] if service == "payments" else []), \
                 patch("workbench.get_snapshot", return_value={**self.metrics, "environment_id": environment_id}), self.assertRaises(error):
                self.workbench.request_recovery({"source": self.source, "incident_id": self.incident})
        self.assertTrue(self.flag.is_file())
        self.assertFalse((self.workbench.root / "recovery/incidents.sqlite3").exists())

    def test_raw_fault_off_does_not_bypass_missing_incident_evidence(self):
        with self.assertRaises(ValueError):
            self.workbench.action({"action": "fault", "instance": self.alias, "service": self.service, "mode": "off"})
        self.assertTrue(self.flag.is_file())

    def test_only_two_disjoint_persisted_post_execution_windows_close_the_ledger(self):
        request = self.request()
        result = self.approve(request)
        cleared = result["execution"]["effect"]["cleared_at"]
        worker = self.workbench.root / "verification-worker"
        worker.mkdir()
        from patrol import connect
        db = connect(worker)
        sessions_path = worker / "hermes/state.db"
        sessions_path.parent.mkdir()
        with sqlite3.connect(sessions_path) as sessions:
            sessions.execute("CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,tool_name TEXT,content TEXT)")

        def window(identifier, start, end, **overrides):
            observed = {**self.metrics, "window_start": start, "window_end": end, "error_rate": 0, **overrides}
            tool_item, report_item = identifier * 2, identifier * 2 + 1
            db.execute("INSERT INTO inspections VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, self.service, "lab", "local", end,
                       "healthy", json.dumps(observed), "healthy", None, "verification-session", "turn-" + str(identifier), tool_item, report_item))
            db.commit()
            with sqlite3.connect(sessions_path) as sessions:
                sessions.executemany("INSERT INTO messages VALUES(?,?,?,?,?)", [
                    (tool_item, "verification-session", "tool", "service_snapshot", json.dumps(observed)),
                    (report_item, "verification-session", "assistant", None, '{"status":"healthy"}')])

        record = {"incident_id": self.incident, "observation": json.dumps(self.metrics), "recovered_inspection_id": 2}
        try:
            window(1, cleared - 60, cleared - 30)
            window(2, cleared + 1, cleared + 31)
            window(3, cleared + 2, cleared + 32)
            window(5, cleared + 33, cleared + 34)
            window(6, cleared + 33, cleared + 63, service="orders")
            self.workbench.sync_recovery_verification(self.source, worker, record)
            self.assertEqual(self.workbench.recovery_snapshot(self.incident)["runtime_state"], "VERIFYING")
            window(4, cleared + 33, cleared + 63)
            self.workbench.sync_recovery_verification("other-run/" + self.alias, worker, record)
            self.assertEqual(self.workbench.recovery_snapshot(self.incident)["runtime_state"], "VERIFYING")
            self.workbench.sync_recovery_verification(self.source, worker, record)
            self.assertEqual(self.workbench.recovery_snapshot(self.incident)["runtime_state"], "CLOSED")
            self.assertEqual(self.workbench.recovery_snapshot(self.incident)["status"], "recovered")
        finally:
            db.close()


class BusinessObservationTests(WorkbenchTestCase):
    def test_resumed_alert_uses_active_run_progress_instead_of_old_run_status(self):
        scope = "environment-current"
        event = {"incident_id": "lab-orders-0123456789ab", "service": "orders",
                 "environment_id": scope, "emitted_at": 200, "source": "lab_monitor"}
        directory = self.workbench.root / "environments" / "instance-b" / "alerts" / "orders"
        directory.mkdir(parents=True)
        (directory / (event["incident_id"] + ".json")).write_text(json.dumps(event))
        instances = [{"id": "instance-b", "services": [{"name": "orders", "metrics": {"environment_id": scope}}]}]
        old = {"incident_id": event["incident_id"], "instance": "instance-b", "service": "orders",
               "observation": {"environment_id": scope}, "source": "old-run/instance-b",
               "status": "investigating", "session_id": "old-session", "report_item_id": 10}
        current = {**old, "source": "active-run/instance-b", "status": "handed_off",
                   "session_id": "current-session", "report_item_id": 20}

        def logs(_state, service):
            return {"source": "live_lab_http_request_logs", "environment_id": scope,
                    "service": service, "entries": []}

        self.workbench.active_run = self.workbench.root / "runs" / "active-run"
        with patch("environment.get_request_logs", side_effect=logs), patch("environment.read_changes", return_value=[]):
            for rows in ([current, old], [old, current]):
                with self.subTest(order=[row["source"] for row in rows]):
                    alert = self.workbench.business_snapshot(instances, rows)["alerts"][0]
                    self.assertEqual(alert["status"], "handed_off")
                    self.assertEqual(alert["report_source"], current["source"])
                    self.assertEqual(alert["report_item_id"], 20)
            self.workbench.active_run = None
            alert = self.workbench.business_snapshot(instances, [current, old])["alerts"][0]
            self.assertEqual(alert["status"], "handed_off")
            wrong_instance = {**current, "instance": "instance-a", "status": "recovered"}
            alert = self.workbench.business_snapshot(instances, [wrong_instance, old])["alerts"][0]
            self.assertEqual(alert["status"], "investigating")
            wrong_scope = {**current, "observation": {"environment_id": "old-environment"}}
            alert = self.workbench.business_snapshot(instances, [wrong_scope])["alerts"][0]
            self.assertEqual(alert["status"], "open")

    def test_business_receipts_preserve_scope_and_do_not_need_an_online_agent(self):
        environment_id = "environment-current"
        instances = [{"id": "instance-a", "services": [
            {"name": "payments", "metrics": {"environment_id": environment_id}}]}]
        state = self.workbench.root / "environments" / "instance-a"
        alerts = state / "alerts" / "payments"
        alerts.mkdir(parents=True)
        event = {"incident_id": "lab-payments-0123456789ab", "service": "payments",
                 "environment_id": environment_id, "emitted_at": 200, "source": "lab_monitor"}
        (alerts / (event["incident_id"] + ".json")).write_text(json.dumps(event))
        old = {**event, "incident_id": "lab-payments-000000000000", "environment_id": "old-environment"}
        (alerts / (old["incident_id"] + ".json")).write_text(json.dumps(old))

        def request_logs(_state, service):
            return {"source": "live_lab_http_request_logs", "environment_id": environment_id,
                    "service": service, "entries": [{"id": "receipt-" + service, "time": 201,
                        "service": service, "environment_id": environment_id, "method": "GET",
                        "path": "/work", "status": 503, "duration_ms": 24, "probe": False}]}

        changes = [{"id": "change-1", "source": "lab-environment-change", "type": "fault_changed",
                    "time": 199, "environment_id": environment_id, "service": "payments",
                    "actor": "lab-operator", "before": "off", "after": "on", "summary": "设置持续故障"}]
        with patch("environment.get_request_logs", side_effect=request_logs), patch("environment.read_changes", return_value=changes) as read_changes:
            result = self.workbench.business_snapshot(instances, [])
        self.assertIsNone(self.workbench.agent)
        self.assertEqual(len(result["alerts"]), 1)
        self.assertEqual(result["alerts"][0]["status"], "open")
        self.assertIsNone(result["alerts"][0]["report_item_id"])
        self.assertTrue(all(row["http_status"] == 503 and row["latency_ms"] == 24 for row in result["logs"]))
        self.assertEqual(result["changes"][0]["kind"], "fault_changed")
        self.assertEqual(result["changes"][0]["after"], "on")
        self.assertEqual(result["errors"], [])
        read_changes.assert_called_once_with(state, environment_id=environment_id, limit=100)

    def test_foreign_environment_receipts_are_not_displayed_as_current(self):
        instances = [{"id": "instance-a", "services": [
            {"name": "payments", "metrics": {"environment_id": "current"}}]}]
        with patch("environment.get_request_logs", return_value={"service": "payments", "environment_id": "old"}), \
             patch("environment.read_changes", return_value=[{"environment_id": "old"}]):
            result = self.workbench.business_snapshot(instances, [])
        self.assertEqual(result["logs"], [])
        self.assertEqual(result["changes"], [])
        self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()

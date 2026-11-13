"""Route integration controls using real local records/SQLite, never native inference.

Only child-process creation is replaced. Session databases below are deliberately
small control fixtures; they are not evidence that Hermes ran a native task.
"""

from contextlib import contextmanager
from http.client import HTTPConnection
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

from test_workbench import WorkbenchTestCase
from workbench import ALIASES, ONCALL_SKILL, file_hash, make_server
import workbench_evolution as evolution
import workbench_rollback as rollback


class CoverageRouteTests(WorkbenchTestCase):
    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def process(self, *, running=True):
        process = Mock(returncode=None if running else 0, pid=os.getpid())
        process.poll.return_value = None if running else 0
        return process

    def online(self, *, stage=3, status="running", running=True):
        run = self.workbench.root / "runs" / "coverage-control"
        run.mkdir(parents=True, exist_ok=True)
        self.write(run / "service.json", {"status": status})
        self.workbench.active_run = run
        self.workbench.agent = self.process(running=running)
        self.workbench.record.update(active_run=run.name, active_stage=stage)
        return run

    def session_database(self, directory, identifier):
        database = directory / "hermes/state.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT,
                    started_at REAL, model TEXT, message_count INTEGER);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,
                    role TEXT, content TEXT, tool_call_id TEXT, tool_name TEXT,
                    tool_calls TEXT);
            """)
            db.execute("INSERT INTO sessions VALUES (?, NULL, 1, 'sqlite-control-fixture', 1)",
                       (identifier,))
            db.execute("INSERT INTO messages VALUES (1, ?, 'assistant', ?, NULL, NULL, '[]')",
                       (identifier, "control report: " + identifier))
        return database

    def test_background_review_db_without_sessions_is_not_a_session_source(self):
        directory = self.workbench.root / "evolution/diagnostic-memory/instance-a/payments/review/worker"
        database = directory / "hermes/state.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE memory(key TEXT PRIMARY KEY, body TEXT)")
        self.assertFalse(self.workbench.persisted_session_database(database))
        self.assertNotIn("evolution:" + directory.relative_to(self.workbench.root).as_posix(), self.workbench.sources())

    def test_frozen_replay_sessions_are_not_online_incidents(self):
        directory = self.workbench.root / "rollbacks" / ("a" * 32) / "behavior-replay/attempt/instance-a/restored"
        self.session_database(directory, "frozen-control")
        source = "evolution:" + directory.relative_to(self.workbench.root).as_posix()
        with patch.object(self.workbench, "incident_snapshot") as online:
            status = self.snapshot()
        online.assert_not_called()
        self.assertEqual(status["incidents"], [])
        self.assertTrue(any(item["source"] == source for item in status["sessions"]))

    def evolution_records(self):
        root = self.workbench.root
        sequence = root / "evolution/diagnostic-memory/instance-a/payments"
        task = sequence / "before/task-one"
        artifact = task / "result.json"
        result = {"session_id": "diagnostic-control", "native_memory_loaded": True}
        self.write(artifact, result)
        self.session_database(task, result["session_id"])
        self.write(sequence / "sequence.json", {
            "schema": evolution.SCHEMA, "instance": "instance-a", "service": "payments",
            "status": "before", "attempts": [], "stages": {"before": {
                "artifact": str(artifact.relative_to(sequence)), "sha256": file_hash(artifact),
                "result": result,
            }},
        })

        identifier = "a" * 32
        request = root / "rollbacks" / identifier
        self.write(request / "request.json", {"schema": rollback.SCHEMA, "id": identifier,
                                              "requested_at": 1})
        # A different attempt exists first. Resolution must follow the recorded
        # Session, not just whichever verification database is discovered first.
        self.session_database(request / "verification/attempt-0/instance-a", "unrelated-control")
        actual = request / "verification/attempt-1/instance-a"
        self.session_database(actual, "rollback-control")
        self.write(request / "result.json", {"status": "verified", "verifications": {
            "instance-a": {"session_id": "rollback-control", "passed": True},
        }})
        return task, actual

    def rollback_fixture(self):
        run = self.online(stage=4)
        environments = {}
        for alias in ALIASES:
            environment = self.workbench.root / "environments" / alias
            environment.mkdir(parents=True)
            environments[alias] = str(environment)
        versions = {}
        for label in ("previous", "adopted"):
            content = ("# " + label + " control Skill\n").encode()
            digest = hashlib.sha256(content).hexdigest()
            path = run / "formal/versions" / digest / ONCALL_SKILL / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            versions[label] = (digest, content)
        active, prior = versions["adopted"][0], versions["previous"][0]
        for alias in ALIASES:
            receiver = run / "receivers" / alias / ONCALL_SKILL / "SKILL.md"
            receiver.parent.mkdir(parents=True)
            receiver.write_bytes(versions["adopted"][1])
        self.write(run / "service.json", {
            "status": "running", "oncall_enabled": True, "learning_enabled": True,
            "mode": "local-script-model", "environments": environments,
            # current_run only probes process existence with signal 0. This is
            # the current test process, not a claimed native On-call worker.
            "workers": {alias: os.getpid() for alias in ALIASES},
        })
        self.write(run / "learning/state.json", {
            "governance_version": "sqlite-v1", "active_sha256": active,
            "last_release": {"status": "adopted", "sha256": active,
                             "previous_sha256": prior,
                             "adoptions": {alias: {"passed": True} for alias in ALIASES}},
        })
        self.write(self.workbench.record_path, self.workbench.record)
        request = self.workbench.action({"action": "rollback_prepare"})
        body = {"action": "rollback_approve", "request_id": request["id"], "confirmed": True,
                "from_sha256": active, "to_sha256": prior}
        return run, request, body

    @contextmanager
    def http_server(self):
        html = self.directory / "coverage.html"
        html.write_text("<p>Route control fixture</p>", encoding="utf-8")
        server = make_server(self.workbench, "127.0.0.1", 0, html)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()

    def post(self, server, body, origin):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("POST", "/api/actions", json.dumps(body),
                               {"Content-Type": "application/json", "Origin": origin})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_diagnostic_requires_a_running_agent_with_memory_stage(self):
        cases = [(2, "running", True), (3, "starting", True),
                 (4, "running", False), (0, "running", True)]
        with patch.object(self.workbench, "spawn") as spawn:
            with self.assertRaisesRegex(ValueError, "Memory"):
                self.workbench.action({"action": "diagnostic_prepare"})
            for stage, status, running in cases:
                with self.subTest(stage=stage, status=status, running=running):
                    self.online(stage=stage, status=status, running=running)
                    with self.assertRaisesRegex(ValueError, "Memory"):
                        self.workbench.action({"action": "diagnostic_prepare"})
            spawn.assert_not_called()
        self.assertEqual(self.workbench.record["jobs"], [])

    def test_unknown_diagnostic_action_does_not_spawn(self):
        self.online()
        with patch.object(self.workbench, "spawn") as spawn:
            with self.assertRaisesRegex(ValueError, "Unknown diagnostic"):
                self.workbench.action({"action": "diagnostic_delete_everything"})
            spawn.assert_not_called()

    def test_diagnostic_finishes_one_requested_stage_without_starting_the_next(self):
        self.online(stage=3)
        process = self.process()
        with patch.object(self.workbench, "spawn", return_value=process) as spawn:
            job = self.workbench.action({"action": "diagnostic_prepare"})
            command = spawn.call_args.args[0]
            self.assertEqual(command[command.index("--action") + 1], "diagnostic_prepare")
            self.assertEqual(command[command.index("--state") + 1], str(self.workbench.root))
            self.assertNotIn("--live", command)
            with self.assertRaisesRegex(ValueError, "同类任务"):
                self.workbench.action({"action": "diagnostic_migrate"})
            process.poll.return_value = process.returncode = 0
            self.workbench.refresh_jobs()
            self.workbench.evolution_snapshot()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual([item["action"] for item in self.workbench.record["jobs"]], ["diagnostic_prepare"])
            self.workbench.action({"action": "diagnostic_migrate"})
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(spawn.call_args.args[0][-1], "diagnostic_migrate")

    def test_evolution_sources_open_the_recorded_diagnostic_and_rollback_sessions(self):
        diagnostic, restored = self.evolution_records()
        state = self.workbench.evolution_snapshot()
        expected = [(state["diagnostic"]["stages"]["before"]["source"], diagnostic, "diagnostic-control"),
                    (state["rollback"]["requests"][0]["result"]["verifications"]["instance-a"]["source"],
                     restored, "rollback-control")]
        sources = self.workbench.sources()
        for source, directory, session in expected:
            with self.subTest(source=source):
                self.assertEqual(sources[source], directory)
                result = self.workbench.session(source, session, 1)
                self.assertEqual(result["messages"][-1]["content"], "control report: " + session)
                with self.assertRaisesRegex(ValueError, "Unknown session"):
                    self.workbench.session(source, "foreign-session")

    def test_snapshot_includes_evolution_stages_and_their_session_sources(self):
        self.evolution_records()
        result = self.snapshot()
        stage = result["evolution"]["diagnostic"]["stages"]["before"]
        self.assertTrue(result["evolution"]["diagnostic"]["evidence_valid"])
        self.assertEqual(stage["result"]["session_id"], "diagnostic-control")
        self.assertTrue(any(item["source"] == stage["source"] and item["id"] == "diagnostic-control"
                            for item in result["sessions"]))
        proof = result["evolution"]["rollback"]["requests"][0]["result"]["verifications"]["instance-a"]
        self.assertTrue(any(item["source"] == proof["source"] and item["id"] == "rollback-control"
                            for item in result["sessions"]))
        self.assertEqual(result["evolution"]["jobs"], [])

    def test_incomplete_rollback_attempt_does_not_hide_the_successful_retry(self):
        _, restored = self.evolution_records()
        incomplete = restored.parent.parent / "attempt-0/instance-a/hermes/state.db"
        for kind in ("empty", "non-session", "corrupt"):
            with self.subTest(kind=kind):
                incomplete.unlink()
                if kind == "corrupt":
                    incomplete.write_bytes(b"interrupted database write")
                else:
                    with sqlite3.connect(incomplete) as db:
                        if kind == "non-session":
                            db.execute("CREATE TABLE async_delegations(id TEXT)")
                before = incomplete.read_bytes()
                result = self.snapshot()
                proof = result["evolution"]["rollback"]["requests"][0]["result"]["verifications"]["instance-a"]
                expected = "evolution:" + restored.relative_to(self.workbench.root).as_posix()
                self.assertEqual(proof["source"], expected)
                self.assertEqual(result["errors"], [])
                self.assertEqual(self.workbench.session(expected, proof["session_id"], 1)["anchor_item_id"], 1)
                self.assertEqual(incomplete.read_bytes(), before)

    def test_evolution_sources_reject_external_database_symlinks(self):
        for layout in ("diagnostic", "rollback"):
            for linked in ("hermes", "state.db"):
                with self.subTest(layout=layout, linked=linked):
                    label = layout + "-" + linked
                    outside = self.directory / ("outside-" + label)
                    external_database = self.session_database(outside, "external-control")
                    before = external_database.read_bytes()
                    relative = ("evolution/diagnostic-memory/instance-a/payments/before/" + label
                                if layout == "diagnostic" else
                                "rollbacks/" + "b" * 32 + "/verification/" + label + "/instance-a")
                    owned = self.workbench.root / relative
                    owned.mkdir(parents=True)
                    if linked == "hermes":
                        (owned / "hermes").symlink_to(outside / "hermes", target_is_directory=True)
                    else:
                        (owned / "hermes").mkdir()
                        (owned / "hermes/state.db").symlink_to(external_database)
                    source = "evolution:" + relative
                    self.assertNotIn(source, self.workbench.sources())
                    with self.assertRaisesRegex(ValueError, "Unknown session source"):
                        self.workbench.session(source, "external-control")
                    self.assertEqual(external_database.read_bytes(), before)

    def test_evolution_stage_links_reject_external_databases(self):
        diagnostic, restored = self.evolution_records()
        for directory, identifier in ((diagnostic, "diagnostic-control"), (restored, "rollback-control")):
            external_database = self.session_database(self.directory / ("outside-" + identifier), identifier)
            (directory / "hermes/state.db").unlink()
            (directory / "hermes/state.db").symlink_to(external_database)
        result = self.workbench.evolution_snapshot()
        self.assertNotIn("source", result["diagnostic"]["stages"]["before"])
        proof = result["rollback"]["requests"][0]["result"]["verifications"]["instance-a"]
        self.assertNotIn("source", proof)

    def test_invalid_diagnostic_artifact_remains_visible_without_breaking_snapshot(self):
        self.evolution_records()
        sequence = self.workbench.root / "evolution/diagnostic-memory/instance-a/payments/sequence.json"
        state = json.loads(sequence.read_text())
        state["stages"]["before"]["artifact"] = str(self.directory / "outside/result.json")
        self.write(sequence, state)
        result = self.snapshot()
        self.assertFalse(result["evolution"]["diagnostic"]["evidence_valid"])
        self.assertIn("last_error", result["evolution"]["diagnostic"])
        self.assertNotIn("source", result["evolution"]["diagnostic"]["stages"]["before"])

    def test_rollback_preparation_stops_at_approval_without_spawning(self):
        with patch.object(self.workbench, "spawn") as spawn:
            run, request, _ = self.rollback_fixture()
            spawn.assert_not_called()
        self.assertEqual(request["status"], "awaiting_approval")
        self.assertFalse((self.workbench.root / "rollbacks" / request["id"] / "approval.json").exists())
        self.assertEqual(json.loads((run / "learning/state.json").read_text())["active_sha256"], request["from_sha256"])
        self.assertEqual(self.workbench.record["jobs"], [])

    def test_rollback_approval_requires_literal_confirmation(self):
        _, request, body = self.rollback_fixture()
        with patch.object(self.workbench, "spawn") as spawn:
            for confirmed in (None, False, 0, 1, "true"):
                with self.subTest(confirmed=confirmed), self.assertRaisesRegex(ValueError, "明确批准"):
                    self.workbench.action({**body, "confirmed": confirmed})
            spawn.assert_not_called()
        self.assertFalse((self.workbench.root / "rollbacks" / request["id"] / "approval.json").exists())

    def test_rollback_approval_rejects_missing_or_stale_hashes_before_spawning(self):
        _, request, body = self.rollback_fixture()
        variants = [{"from_sha256": None}, {"to_sha256": None},
                    {"from_sha256": "0" * 64}, {"to_sha256": "0" * 64}]
        with patch.object(self.workbench, "spawn") as spawn:
            for changed in variants:
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "both displayed Skill hashes"):
                    self.workbench.action({**body, **changed})
            spawn.assert_not_called()
        self.assertFalse((self.workbench.root / "rollbacks" / request["id"] / "approval.json").exists())

    def test_rollback_approval_rejects_a_release_changed_after_preparation(self):
        run, request, body = self.rollback_fixture()
        path = run / "learning/state.json"
        state = json.loads(path.read_text())
        state["last_release"]["status"] = "pending_adoption"
        self.write(path, state)
        with patch.object(self.workbench, "spawn") as spawn:
            with self.assertRaisesRegex(ValueError, "adopted by both"):
                self.workbench.action(body)
            spawn.assert_not_called()
        self.assertFalse((self.workbench.root / "rollbacks" / request["id"] / "approval.json").exists())

    def test_http_rollback_approval_carries_context_and_both_hashes_then_schedules_only_execute(self):
        _, request, body = self.rollback_fixture()
        with self.http_server() as server, patch.object(self.workbench, "spawn", return_value=self.process()) as spawn, \
                patch.object(rollback, "approve", wraps=rollback.approve) as approve:
            authority = f"127.0.0.1:{server.server_port}"
            status, result = self.post(server, body, "https://foreign.example")
            self.assertEqual(status, 403)
            approve.assert_not_called()
            spawn.assert_not_called()
            status, result = self.post(server, {**body, "actor": "untrusted-client"}, "http://" + authority)
            self.assertEqual(status, 200, result)
            approve.assert_called_once_with(self.workbench.root, request["id"], body["from_sha256"], body["to_sha256"],
                                            request_context={"transport": "http", "origin": "http://" + authority,
                                                             "host": authority})
            spawn.assert_called_once()
            command = spawn.call_args.args[0]
            self.assertEqual(Path(command[2]).name, "workbench_rollback.py")
            self.assertEqual(command[command.index("--action") + 1], "execute")
            self.assertEqual(command[command.index("--request-id") + 1], request["id"])
            self.assertEqual(result["kind"], "rollback")
            self.assertEqual(result["request_id"], request["id"])
        approval = json.loads((self.workbench.root / "rollbacks" / request["id"] / "approval.json").read_text())
        self.assertEqual(approval["from_sha256"], body["from_sha256"])
        self.assertEqual(approval["to_sha256"], body["to_sha256"])
        self.assertEqual(approval["source"], "local_workbench_http")
        self.assertEqual(approval["actor"], "local-workbench-user")


if __name__ == "__main__":
    unittest.main(verbosity=2)

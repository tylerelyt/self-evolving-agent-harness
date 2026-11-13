"""Require explicit version-bound approval and actual rollback execution evidence."""

from copy import deepcopy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workbench_rollback as rollback
import oncall_service
from environment import load_json, write_json
from oncall_service import replace_skill


class RollbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "runs" / "active"
        (self.run / "learning").mkdir(parents=True)
        write_json(self.root / "workbench.json", {"active_run": "active"})
        self.previous = b"---\nname: oncall-alert-triage\n---\nPrevious approved rule.\n"
        self.active = self.previous + b"Adopted rule.\n"
        self.previous_sha = rollback.hashlib.sha256(self.previous).hexdigest()
        self.active_sha = rollback.hashlib.sha256(self.active).hexdigest()
        for digest, content in ((self.previous_sha, self.previous), (self.active_sha, self.active)):
            replace_skill(self.run / "formal" / "versions" / digest / rollback.ONCALL_SKILL / "SKILL.md", content)
        for alias in rollback.ALIASES:
            replace_skill(self.run / "receivers" / alias / rollback.ONCALL_SKILL / "SKILL.md", self.active)
        self.service = {
            "status": "running", "oncall_enabled": True, "mode": "local-script-model",
            "workers": dict.fromkeys(rollback.ALIASES, os.getpid()),
            "environments": {alias: str(self.root / "environments" / alias) for alias in rollback.ALIASES},
        }
        write_json(self.run / "service.json", self.service)
        self.state = {
            "status": "waiting", "active_sha256": self.active_sha, "attempts": {"batch": 1},
            "processed_sessions": ["source-a", "source-b"],
            "last_release": {"status": "adopted", "sha256": self.active_sha,
                             "previous_sha256": self.previous_sha,
                             "adoptions": {alias: {"passed": True} for alias in rollback.ALIASES}},
        }
        self.state_path = self.run / "learning" / "state.json"
        write_json(self.state_path, self.state)
        checkpoints = {alias: {"id": 10, "session_id": "patrol-" + alias} for alias in rollback.ALIASES}
        before = patch.object(rollback, "patrol_checkpoints", return_value=checkpoints)
        after = patch.object(rollback, "continued_patrol", return_value={
            alias: {**row, "id": 11} for alias, row in checkpoints.items()
        })
        before.start()
        after.start()
        behavior = patch.object(rollback, "replay_behavior", return_value={
            alias: {"source": "frozen_incident_replay", "behavior_restored": True} for alias in rollback.ALIASES
        })
        behavior.start()
        self.addCleanup(before.stop)
        self.addCleanup(after.stop)
        self.addCleanup(behavior.stop)

    def approval(self):
        request = rollback.prepare(self.root)
        rollback.approve(self.root, request["id"], self.active_sha, self.previous_sha)
        return request

    def fake_publish(self, root, source, digest, *, state, safe_sha256):
        self.assertEqual(safe_sha256, self.previous_sha)
        self.assertEqual(load_json(self.state_path)["operator_rollback"]["status"], "awaiting_verification")
        for alias in rollback.ALIASES:
            replace_skill(root / "receivers" / alias / rollback.ONCALL_SKILL / "SKILL.md", source.read_bytes())
        return {"sha256": digest, "receivers": dict.fromkeys(rollback.ALIASES, {"sha256": digest})}

    def fake_verify(self, directory, source, environment, service, digest, options):
        self.assertEqual(source.read_bytes(), self.previous)
        self.assertEqual(digest, self.previous_sha)
        return {"session_id": environment.name + "-new-session", "passed": True,
                "skill_sha256": digest, "probe_http_statuses": [200]}

    def interrupt_completion(self, point):
        """Deliver a control SIGINT at a real persistence boundary, not a native task."""
        request = self.approval()
        result_path = self.root / "rollbacks" / request["id"] / "result.json"
        original_write = rollback.write_json

        def interrupted_write(path, content):
            final_state = (Path(path).resolve() == self.state_path.resolve()
                           and content.get("operator_rollback", {}).get("status") == "verified")
            exposed_result = Path(path).resolve() == result_path.resolve() and content.get("status") == "verified"
            if (point == "authority" and final_state) or (point == "result" and exposed_result):
                raise KeyboardInterrupt("completion commit interrupted")
            return original_write(path, content)

        with (patch.object(rollback, "publish", side_effect=self.fake_publish),
              patch.object(rollback, "verify_task", side_effect=self.fake_verify),
              patch.object(rollback, "scripted_model", return_value=nullcontext("http://unused.invalid")),
              patch.object(rollback, "write_json", side_effect=interrupted_write)):
            with self.assertRaisesRegex(KeyboardInterrupt, "commit interrupted"):
                rollback.execute(self.root, request["id"])
        return request

    def reconcile_without_native_work(self, request):
        with (patch.object(rollback, "publish", side_effect=AssertionError("must not republish")) as publish,
              patch.object(rollback, "verify_task", side_effect=AssertionError("must not rerun tasks")) as verify,
              patch.object(rollback, "replay_behavior", side_effect=AssertionError("must not replay")) as replay,
              patch.object(rollback, "patrol_checkpoints", side_effect=AssertionError("completion already checked patrol"))):
            result = rollback.execute(self.root, request["id"])
        publish.assert_not_called()
        verify.assert_not_called()
        replay.assert_not_called()
        return result

    def test_prepare_and_approve_have_no_publication_side_effect(self):
        before = self.state_path.read_bytes()
        with patch.object(rollback, "publish") as publish:
            request = rollback.prepare(self.root)
            with self.assertRaises(FileNotFoundError):
                rollback.execute(self.root, request["id"])
            with self.assertRaisesRegex(ValueError, "both displayed"):
                rollback.approve(self.root, request["id"], self.previous_sha, self.active_sha)
            rollback.approve(self.root, request["id"], self.active_sha, self.previous_sha)
        publish.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(rollback.rollback_state(self.root)["requests"][0]["status"], "approved")

    def test_requests_are_listed_in_timestamp_order_instead_of_uuid_order(self):
        requests = sorted([rollback.prepare(self.root), rollback.prepare(self.root)], key=lambda row: row["id"])
        for request, timestamp in zip(requests, (200, 100)):
            request["requested_at"] = timestamp
            write_json(self.root / "rollbacks" / request["id"] / "request.json", request)
        listed = rollback.rollback_state(self.root)["requests"]
        self.assertEqual([row["requested_at"] for row in listed], [100, 200])
        self.assertEqual(listed[-1]["id"], requests[0]["id"])

    def test_changed_active_run_or_hash_blocks_approval_and_execution(self):
        request = self.approval()
        changed = {**self.state, "active_sha256": self.previous_sha}
        write_json(self.state_path, changed)
        with patch.object(rollback, "publish") as publish:
            with self.assertRaises(ValueError):
                rollback.execute(self.root, request["id"])
        publish.assert_not_called()
        write_json(self.root / "workbench.json", {"active_run": "other"})
        with self.assertRaisesRegex(ValueError, "active On-call run changed"):
            rollback.execute(self.root, request["id"])

    def test_unknown_snapshot_and_other_version_approval_are_rejected(self):
        request = self.approval()
        directory = self.root / "rollbacks" / request["id"]
        approval = load_json(directory / "approval.json")
        write_json(directory / "approval.json", {**approval, "to_sha256": self.active_sha})
        with patch.object(rollback, "publish") as publish:
            with self.assertRaisesRegex(ValueError, "explicit local approval"):
                rollback.execute(self.root, request["id"])
            write_json(directory / "approval.json", approval)
            previous = self.run / "formal" / "versions" / self.previous_sha / rollback.ONCALL_SKILL / "SKILL.md"
            previous.write_bytes(b"changed snapshot")
            with self.assertRaisesRegex(ValueError, "snapshot has changed"):
                rollback.execute(self.root, request["id"])
        publish.assert_not_called()

    def test_old_uncooperative_live_learner_is_rejected(self):
        write_json(self.run / "service.json", {**self.service, "learning_enabled": True})
        with self.assertRaisesRegex(ValueError, "Restart the learner"):
            rollback.prepare(self.root)
        self.assertFalse((self.root / "rollbacks").exists())

    def test_success_requires_two_independent_tasks_and_preserves_history(self):
        request = self.approval()
        with patch.object(rollback, "publish", side_effect=self.fake_publish) as publish:
            with patch.object(rollback, "verify_task", side_effect=self.fake_verify) as verify:
                result = rollback.execute(self.root, request["id"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(set(result["verifications"]), set(rollback.ALIASES))
        self.assertTrue(all(entry["behavior_restored"] for entry in result["behavior_replay"].values()))
        saved = load_json(self.state_path)
        self.assertEqual(saved["active_sha256"], self.previous_sha)
        self.assertEqual(saved["last_release"]["status"], "rolled_back")
        self.assertEqual(saved["processed_sessions"], self.state["processed_sessions"])
        self.assertEqual(saved["attempts"], self.state["attempts"])
        self.assertEqual(saved["operator_rollback"]["status"], "verified")
        self.assertTrue(all(result["patrol_continued"][alias]["id"] > result["patrol_before"][alias]["id"]
                            for alias in rollback.ALIASES))

    def test_failed_second_task_stays_pending_and_can_be_retried(self):
        request = self.approval()
        def fail_second(*args):
            if args[2].name == "instance-b":
                raise RuntimeError("Native task interrupted")
            return self.fake_verify(*args)
        with patch.object(rollback, "publish", side_effect=self.fake_publish):
            with patch.object(rollback, "verify_task", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    rollback.execute(self.root, request["id"])
            self.assertEqual(load_json(self.state_path)["operator_rollback"]["status"], "awaiting_verification")
            with patch.object(rollback, "verify_task", side_effect=self.fake_verify):
                result = rollback.execute(self.root, request["id"])
        self.assertEqual(result["status"], "verified")

    def test_failed_publication_can_retry_with_the_same_explicit_approval(self):
        request = self.approval()
        approval_path = self.root / "rollbacks" / request["id"] / "approval.json"
        approval = approval_path.read_bytes()
        with patch.object(rollback, "publish", side_effect=RuntimeError("publication unavailable")):
            with self.assertRaisesRegex(RuntimeError, "publication unavailable"):
                rollback.execute(self.root, request["id"])
        pending = load_json(self.state_path)
        self.assertEqual(pending["active_sha256"], self.active_sha)
        self.assertEqual(pending["operator_rollback"]["status"], "awaiting_verification")
        with patch.object(rollback, "publish", side_effect=self.fake_publish):
            with patch.object(rollback, "verify_task", side_effect=self.fake_verify):
                result = rollback.execute(self.root, request["id"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["request_id"], request["id"])
        self.assertEqual(approval_path.read_bytes(), approval)

    def test_interrupt_after_result_keeps_retry_visible_and_reconciles_authority(self):
        request = self.interrupt_completion("authority")
        directory = self.root / "rollbacks" / request["id"]
        result_before = (directory / "result.json").read_bytes()
        self.assertEqual(load_json(directory / "result.json")["status"], "verified")
        self.assertEqual(load_json(self.state_path)["operator_rollback"]["status"], "awaiting_verification")
        self.assertEqual(rollback.rollback_state(self.root)["requests"][0]["status"], "awaiting_verification")
        with (patch.object(oncall_service, "completed_incidents", return_value=[]),
              patch.object(oncall_service, "select_batch", return_value=None) as select):
            oncall_service.learn_once(self.run, False, object(), {})
            select.assert_not_called()
            result = self.reconcile_without_native_work(request)
            oncall_service.learn_once(self.run, False, object(), {})
            select.assert_called_once()
        self.assertEqual(result["status"], "verified")
        self.assertEqual((directory / "result.json").read_bytes(), result_before)
        self.assertEqual(load_json(self.state_path)["operator_rollback"]["status"], "verified")
        self.assertEqual(rollback.rollback_state(self.root)["requests"][0]["status"], "verified")

    def test_interrupt_after_completion_journal_restores_missing_result_without_reexecution(self):
        request = self.interrupt_completion("result")
        directory = self.root / "rollbacks" / request["id"]
        self.assertFalse((directory / "result.json").exists())
        self.assertEqual(rollback.rollback_state(self.root)["requests"][0]["status"], "awaiting_verification")
        result = self.reconcile_without_native_work(request)
        self.assertEqual(load_json(directory / "result.json"), result)
        self.assertEqual(load_json(self.state_path)["operator_rollback"]["status"], "verified")

    def test_changed_completion_evidence_cannot_commit_or_republish(self):
        request = self.interrupt_completion("authority")
        state = load_json(self.state_path)
        artifact = self.root / "rollbacks" / request["id"] / state["operator_rollback"]["completion"]["artifact"]
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        with (patch.object(rollback, "publish") as publish,
              patch.object(rollback, "verify_task") as verify):
            with self.assertRaisesRegex(ValueError, "completion evidence changed"):
                rollback.execute(self.root, request["id"])
        publish.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(load_json(self.state_path)["operator_rollback"]["status"], "awaiting_verification")

    def test_older_verified_request_can_add_frozen_proof_without_republishing(self):
        request = self.approval()
        with patch.object(rollback, "publish", side_effect=self.fake_publish):
            with patch.object(rollback, "verify_task", side_effect=self.fake_verify):
                result = rollback.execute(self.root, request["id"])
        result.pop("behavior_replay")
        directory = self.root / "rollbacks" / request["id"]
        write_json(directory / "result.json", result)
        with patch.object(rollback, "publish") as publish:
            upgraded = rollback.execute(self.root, request["id"])
        publish.assert_not_called()
        self.assertEqual(upgraded["verifications"], result["verifications"])
        self.assertTrue(upgraded["behavior_replay"])


class RollbackEvidenceTests(unittest.TestCase):
    def setUp(self):
        content = "approved skill\n"
        digest = rollback.hashlib.sha256(content.encode()).hexdigest()
        self.task = {"service": "payments", "environment_id": "env", "skill_sha256": digest}
        self.rows = []
        for name, args, value in (
            ("skill_view", {"name": rollback.ONCALL_SKILL},
             {"success": True, "name": rollback.ONCALL_SKILL, "sha256": digest, "content": content}),
            ("service_snapshot", {}, {"service": "payments", "environment_id": "env",
                                     "source": "live_lab_http_requests", "window_end": 100}),
            ("oncall_probe", {}, {"service": "payments", "environment_id": "env", "http_status": 200, "checked_at": 101}),
        ):
            call_id = name + "-call"
            self.rows.extend([
                {"id": len(self.rows) + 1, "role": "assistant", "tool_calls": [{"id": call_id,
                 "function": {"name": name, "arguments": json.dumps(args)}}]},
                {"id": len(self.rows) + 2, "role": "tool", "tool_call_id": call_id,
                 "tool_name": name, "content": json.dumps(value)},
            ])
        self.rows.append({"id": 7, "role": "assistant", "content": json.dumps({**self.task, "probe_http_statuses": [200]})})

    def test_report_and_skill_are_verified_against_actual_sequential_receipts(self):
        self.assertTrue(rollback.verify_rows(self.rows, self.task)["passed"])
        variants = []
        wrong_skill = deepcopy(self.rows)
        wrong_skill[1]["content"] = wrong_skill[1]["content"].replace("approved skill", "other skill")
        variants.append(wrong_skill)
        wrong_scope = deepcopy(self.rows)
        wrong_scope[5]["content"] = wrong_scope[5]["content"].replace('"env"', '"other-env"')
        variants.append(wrong_scope)
        wrong_report = deepcopy(self.rows)
        wrong_report[-1]["content"] = wrong_report[-1]["content"].replace("[200]", "[503]")
        variants.append(wrong_report)
        concurrent = deepcopy(self.rows)
        concurrent[4]["id"] = 3
        variants.append(concurrent)
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                rollback.verify_rows(rows, self.task)


class LearnerGovernanceTests(unittest.TestCase):
    setUp = RollbackTests.setUp

    def test_each_learning_cycle_reloads_the_operator_committed_version(self):
        module = object()
        with patch.object(oncall_service, "completed_incidents", return_value=[]):
            with patch.object(oncall_service, "select_batch", return_value=None):
                oncall_service.learn_once(self.run, False, module, {})
                restored = load_json(self.state_path)
                restored.update(active_sha256=self.previous_sha, operator_rollback={"status": "verified"})
                write_json(self.state_path, restored)
                oncall_service.learn_once(self.run, False, module, {})
        saved = load_json(self.state_path)
        self.assertEqual(saved["active_sha256"], self.previous_sha)
        self.assertEqual(saved["governance_version"], "sqlite-v1")
        self.assertEqual(saved["operator_rollback"], {"status": "verified"})

    def test_pending_operator_verification_blocks_new_candidate_selection(self):
        write_json(self.state_path, {**self.state, "operator_rollback": {"status": "awaiting_verification"}})
        with patch.object(oncall_service, "completed_incidents", return_value=[]):
            with patch.object(oncall_service, "select_batch") as select:
                oncall_service.learn_once(self.run, False, object(), {})
        select.assert_not_called()
        self.assertEqual(load_json(self.state_path)["operator_rollback"]["status"], "awaiting_verification")

    def test_operator_lock_prevents_learner_state_writes(self):
        before = self.state_path.read_bytes()
        with rollback.writer_lock(self.run / "learning", "governance.sqlite"):
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                oncall_service.learn_once(self.run, False, object(), {})
        self.assertEqual(self.state_path.read_bytes(), before)


class BehaviorReplayTests(unittest.TestCase):
    def test_same_captured_case_distinguishes_restored_and_adopted_behavior(self):
        original = {"service": "payments", "environment_id": "original-env", "source": "live_lab_http_requests"}
        source = {"source_snapshot": original, "probe_http_statuses": [503, 200],
                  "incident_id": "original-incident", "source_session_id": "original-session"}
        request = {"to_sha256": "a" * 64, "from_sha256": "b" * 64}

        def replay(state, skill, options, snapshot, statuses):
            restored = state.name == "restored"
            self.assertEqual(snapshot, {**original, "source": rollback.REPLAY_SOURCE})
            self.assertEqual(statuses, [503, 200])
            return {"trace_verified": True, "status": "observed", "session_id": str(state),
                    "skill_sha256": request["to_sha256" if restored else "from_sha256"],
                    "probe_statuses": [503] if restored else [503, 200],
                    "reported_assessment": "failure_observed" if restored else "intermittent"}

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "skill.md").write_text("Restored one-probe Skill.")
            with patch.object(rollback, "frozen_incident", return_value=source):
                with patch.object(rollback, "known_version", return_value=directory / "skill.md"):
                    with patch.object(rollback, "run_replay", side_effect=replay) as run:
                        result = rollback.replay_behavior(directory / "proof", directory, request)
                        self.assertEqual(run.call_count, 4)

                    def repeated_restored(*args):
                        value = replay(*args)
                        if args[0].name == "restored":
                            value.update(probe_statuses=[503, 200], reported_assessment="intermittent")
                        return value

                    with patch.object(rollback, "run_replay", side_effect=repeated_restored):
                        with self.assertRaisesRegex(ValueError, "prior Skill's probe behavior"):
                            rollback.replay_behavior(directory / "mismatch", directory, request)
        self.assertTrue(all(entry["source"] == "frozen_incident_replay"
                            and entry["behavior_restored"] for entry in result.values()))


if __name__ == "__main__":
    unittest.main()

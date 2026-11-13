"""Continuous application integration with the workload-independent RSI core."""

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall import service
from self_evolving_harness.coordinator import ImprovementCoordinator
from self_evolving_harness.learning import LearningLedger


class ContinuousRSITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "learning").mkdir()
        self.parent = service.sha256(service.SEED)
        self.candidate = self.root / "candidate.md"
        self.candidate.write_bytes(service.SEED.read_bytes() + b"\nA candidate method.\n")
        self.new = service.sha256(self.candidate)
        self.parent_file = self.root / "formal/versions" / self.parent / service.ONCALL_SKILL / "SKILL.md"
        service.replace_skill(self.parent_file, service.SEED.read_bytes())
        for alias in service.ALIASES:
            service.replace_skill(self.receiver(alias), service.SEED.read_bytes())
        self.state = {"status": "waiting", "active_sha256": self.parent, "processed_sessions": [], "attempts": {}}
        service.write_json(self.root / "learning/state.json", self.state)
        self.ledger = service.learning_ledger(self.root)

    def receiver(self, alias):
        return self.root / "receivers" / alias / service.ONCALL_SKILL / "SKILL.md"

    def native_delivery_fixture(self, formal, source, identity, receivers):
        service.replace_skill(formal / "versions" / identity / service.ONCALL_SKILL / "SKILL.md", source.read_bytes())
        for directory in receivers.values():
            service.replace_skill(directory / service.ONCALL_SKILL / "SKILL.md", source.read_bytes())
        return {"sha256": identity, "receivers": {alias: {"sha256": identity} for alias in receivers},
                "receiver_errors": {}}

    def report(self):
        return {"passed": True, "baseline_sha256": self.parent, "candidate_sha256": self.new,
                "cases": [{"baseline": {"valid_trace": True, "passed": False},
                           "candidate": {"valid_trace": True, "passed": True}}]}

    def eligible(self):
        identifier = self.ledger.register(scope=self.ledger.namespace, artifact_sha256=self.new,
            parent_sha256=self.parent, sources=["a:session", "b:session"], generator="fixture")
        self.ledger.evaluate(identifier, self.report())
        return identifier

    def test_continuous_learning_registers_and_publishes_through_the_shared_core(self):
        batch = {}
        completed = []
        for alias in service.ALIASES:
            archive = self.root / f"{alias}.json"
            archive.write_text(json.dumps({"session_id": alias}))
            batch[alias] = {"session_id": alias, "archive": str(archive)}
            completed.append({"instance": alias, "record": {"session_id": alias, "incident_id": "incident-" + alias}})
        with patch.object(service, "completed_incidents", return_value=completed), \
             patch.object(service, "select_batch", return_value=batch), \
             patch("engineering_analysis.build_analysis", return_value={}), \
             patch("skillclaw_evolve.evolve", return_value=(self.candidate, {})), \
             patch("fleet_eval.evaluate", return_value=self.report()), \
             patch("skillclaw_evolve.release", side_effect=self.native_delivery_fixture):
            service.learn_once(self.root, False, SimpleNamespace(release_policy=lambda *a: True), {"model": "fixture"})
        state = service.load_json(self.root / "learning/state.json")
        identifier = state["last_release"]["candidate_id"]
        self.assertEqual(self.ledger.get(identifier)["status"], "published")
        self.assertEqual(state["active_sha256"], self.new)
        self.assertNotIn("pending_release", state)
        from self_evolving_harness.control import ControlStore
        jobs = ControlStore(self.root / "system/control.sqlite3")
        import sqlite3
        with sqlite3.connect(jobs.path) as db:
            row = db.execute("SELECT id,status,attempts FROM tasks").fetchone()
        self.assertEqual(row[1:],("completed",1))
        events = jobs.history(row[0])
        self.assertEqual(events[-1]["kind"],"completed")
        stages = [event["body"]["value"]["stage"] for event in events if event["kind"]=="checkpoint"]
        self.assertEqual(stages,["generating","evaluating","publishing"])

    def test_learning_retry_uses_durable_backoff_and_bounded_attempts(self):
        import sqlite3
        from self_evolving_harness.control import ControlStore
        batch,completed = {},[]
        for alias in service.ALIASES:
            archive = self.root / f"{alias}.json"
            archive.write_text(json.dumps({"session_id":alias}))
            batch[alias] = {"session_id":alias,"archive":str(archive)}
            completed.append({"instance":alias,"record":{"session_id":alias,"incident_id":"i-"+alias}})
        jobs = ControlStore(self.root / "system/control.sqlite3")
        with patch.object(service,"completed_incidents",return_value=completed), \
             patch.object(service,"select_batch",return_value=batch), \
             patch("engineering_analysis.build_analysis",return_value={}), \
             patch("skillclaw_evolve.evolve",side_effect=RuntimeError("provider unavailable")) as evolve:
            service.learn_once(self.root,False,SimpleNamespace(),{"model":"fixture"})
            service.learn_once(self.root,False,SimpleNamespace(),{"model":"fixture"})
            self.assertEqual(evolve.call_count,1)  # Persisted retry delay is respected.
            for _ in range(2):
                with sqlite3.connect(jobs.path) as db:
                    db.execute("UPDATE tasks SET available_at=0")
                service.learn_once(self.root,False,SimpleNamespace(),{"model":"fixture"})
            self.assertEqual(evolve.call_count,3)
        with sqlite3.connect(jobs.path) as db:
            row = db.execute("SELECT status,attempts FROM tasks").fetchone()
        self.assertEqual(row,("failed",3))
        self.assertEqual(set(service.load_json(self.root / "learning/state.json")["processed_sessions"]),
                         set(service.ALIASES))

    def test_interrupted_delivery_is_restored_and_recorded_after_restart(self):
        identifier = self.eligible()
        coordinator = ImprovementCoordinator(self.ledger)
        original = service.replace_skill

        def interrupt(path, content):
            if path == self.receiver("instance-b"):
                raise SystemExit("power loss fixture")
            original(path, content)

        with patch("skillclaw_evolve.release", side_effect=self.native_delivery_fixture):
            with patch.object(service, "replace_skill", side_effect=interrupt), self.assertRaises(SystemExit):
                coordinator.publish(identifier, self.candidate, list(service.ALIASES),
                    lambda: service.publish(self.root, self.candidate, self.new, state=self.state, candidate_id=identifier))
            self.assertEqual(self.ledger.get(identifier)["status"], "publishing")
            reopened = service.load_json(self.root / "learning/state.json")
            service.recover_publication(self.root, reopened)
        self.assertEqual(LearningLedger(self.ledger.path).get(identifier)["status"], "rolled_back")
        self.assertEqual([service.sha256(self.receiver(alias)) for alias in service.ALIASES], [self.parent, self.parent])
        self.assertNotIn("pending_release", reopened)

    def test_intent_before_delivery_is_reconciled_without_republishing(self):
        identifier = self.eligible()
        self.ledger.begin_publication(identifier, list(service.ALIASES))
        with patch.object(service, "publish") as publish:
            service.reconcile_learning_ledger(self.root, self.state)
        publish.assert_not_called()
        self.assertEqual(self.ledger.get(identifier)["status"], "rolled_back")

    def test_mutated_artifact_cannot_reach_the_publication_callback(self):
        identifier = self.eligible()
        self.candidate.write_bytes(b"unapproved bytes")
        with patch.object(service, "publish") as publish, self.assertRaisesRegex(ValueError, "changed"):
            ImprovementCoordinator(self.ledger).publish(identifier, self.candidate, list(service.ALIASES), publish)
        publish.assert_not_called()
        self.assertEqual(self.ledger.get(identifier)["status"], "eligible")

    def test_workspace_identity_survives_relocation_and_attempts_are_distinct(self):
        import shutil
        target = self.root / "moved.sqlite3"
        shutil.copy2(self.ledger.path, target)
        self.assertEqual(LearningLedger(target).namespace, self.ledger.namespace)
        spec = dict(scope=self.ledger.namespace, artifact_sha256=self.new, parent_sha256=self.parent,
                    sources=["same-session"], generator="fixture")
        first = self.ledger.register(**spec, generation={"attempt": 1})
        second = self.ledger.register(**spec, generation={"attempt": 2})
        self.assertNotEqual(first, second)

    def test_v1_ledger_migrates_without_rewriting_candidate_history(self):
        import sqlite3
        identifier = self.eligible()
        before = self.ledger.get(identifier)
        history = self.ledger.history(identifier)
        with sqlite3.connect(self.ledger.path) as db:
            db.execute("DROP TABLE metadata")
            db.execute("PRAGMA user_version=1")
        migrated = LearningLedger(self.ledger.path)
        self.assertEqual(migrated.get(identifier), before)
        self.assertEqual(migrated.history(identifier), history)
        self.assertEqual(migrated.namespace, LearningLedger(self.ledger.path).namespace)


if __name__ == "__main__":
    unittest.main()

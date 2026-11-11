"""Review decisions must not masquerade as published, recallable Memory."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import workbench_memory as memory
from patrol import publish
from shared_facts import publish_fact, read_fact


class MemoryReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run = self.root / "runs" / "first"
        self.worker = self.run / "patrols" / "instance-a"
        self.worker.mkdir(parents=True)
        memory.write_json(self.run / "service.json", {"status": "stopped"})
        self.incident_id = "lab-payments-" + "d" * 12
        self.session = "patrol-payments-" + "a" * 32
        self.branch_session = "patrol-fork-" + "b" * 32
        self.environment_id = "c" * 32
        self.skill = "Pinned triage method"
        self.digest = hashlib.sha256(self.skill.encode()).hexdigest()
        self.fact = {
            "incident_id": self.incident_id, "instance": "instance-a",
            "service": "payments", "environment_id": self.environment_id,
            "source_session_id": self.session, "source_report_item_id": 10,
            "investigation_session_id": self.branch_session, "investigation_report_item_id": 30,
            "probe_statuses": [503, 503, 503], "recovery_end": 120,
            "source_skill_sha256": self.digest,
        }
        self.make_recovered_incident()

    def make_recovered_incident(self):
        """Persist scoped patrol, branch and two clean windows, without a model."""
        incident = {
            "incident_id": self.incident_id, "steer_item_id": 2, "steer_delivery": "callback",
            "probe_item_id": 3, "skill_name": "oncall-alert-triage",
            "skill_sha256": self.digest, "skill_item_id": 1,
        }
        with closing(memory.connect(self.worker)) as db:
            for index, (start, end, error_rate) in enumerate(((0, 30, .5), (50, 80, 0), (90, 120, 0))):
                observation = {
                    "source": "live_lab_http_requests", "service": "payments",
                    "environment_id": self.environment_id, "window_start": start, "window_end": end,
                    "requests": 100, "error_rate": error_rate, "p95_ms": 10,
                }
                report = {"status": "abnormal" if error_rate else "healthy",
                          "summary": "Stored HTTP window", "history_id": None}
                publish(db, "payments", observation, report, self.session, f"turn-{index}",
                        {"id": 9}, {"id": 10 + index}, incident if index == 0 else None,
                        incident if index == 2 else None)

        database = self.worker / "hermes" / "state.db"
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                    tool_name TEXT, tool_call_id TEXT, tool_calls TEXT
                );
            """)
            db.executemany("INSERT INTO sessions VALUES (?, ?)",
                           [(self.session, None), (self.branch_session, self.session)])

            def message(item, session, role, body, *, name=None, call_id=None, calls=()):
                db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (item, session, role, json.dumps(body), name, call_id, json.dumps(calls)))

            def call(item, name, result):
                identifier = f"call-{item}"
                message(item, self.branch_session, "assistant", None,
                        calls=[{"id": identifier, "function": {"name": name, "arguments": "{}"}}])
                message(item + 1, self.branch_session, "tool", result, name=name, call_id=identifier)

            message(1, self.session, "tool", {"sha256": self.digest, "content": self.skill})
            message(10, self.session, "assistant", {"status": "abnormal"})
            message(20, self.branch_session, "assistant", {"status": "abnormal"})
            call(21, "skill_view", {"success": True, "sha256": self.digest})
            probes = []
            for item in (23, 25, 27):
                probe = {"service": "payments", "environment_id": self.environment_id, "http_status": 503}
                call(item, "followup_probe", probe)
                probes.append({**probe, "item_id": item + 1, "call_item_id": item})
            report = {"analysis": "Three persisted probes failed"}
            message(30, self.branch_session, "assistant", report)

        branch = {
            "incident_id": self.incident_id, "service": "payments", "environment_id": self.environment_id,
            "source_session_id": self.session, "source_report_item_id": 10,
            "session_id": self.branch_session, "copied_report_item_id": 20,
            "skill_sha256": self.digest, "skill_item_id": 22, "report_item_id": 30,
            "probes": probes, "report": report,
        }
        inbox = self.worker / "pages" / self.incident_id
        inbox.mkdir(parents=True)
        memory.write_json(inbox / "investigation.json", branch)
        memory.write_json(inbox / "page.json", {
            "incident_id": self.incident_id, "session_id": self.branch_session,
            "report_item_id": 30, "report": report, "paged_at": 40,
        })

    def background_decision(self, status, *, publish=True):
        """Control the external review boundary; publication still uses real SQLite."""
        def decide(attempt, proofs, recovered, shared, live):
            self.assertEqual(proofs["instance-a"]["incident"]["incident_id"], self.incident_id)
            self.assertEqual(recovered["instance-a"]["window_end"], 120)
            memory.write_json(attempt / "decision.json", {"status": status})
            if status == "proposed" and publish:
                publish_fact(shared, self.fact)
            return {"instance-a": {"status": status, "evidence": dict(self.fact)}}
        return decide

    def add_new_empty_run(self):
        new_run = self.root / "runs" / "second"
        new_run.mkdir()
        memory.write_json(new_run / "service.json", {"status": "running"})
        worker = new_run / "patrols" / "instance-a"
        worker.mkdir(parents=True)
        with closing(memory.connect(worker)):
            pass

    def test_keep_is_a_durable_decision_without_a_fact_or_a_recall(self):
        with patch.object(memory, "review_facts", side_effect=self.background_decision("keep")) as model:
            result = memory.review(self.root, self.run, {})
            self.assertEqual(result["status"], "empty")
            self.assertEqual(result["facts"], 0)
            self.assertEqual(result["published"], [])
            self.assertEqual(result["kept"], [self.incident_id])
            saved = memory.load_json(self.root / "memory/state.json")
            self.assertEqual(saved["facts"], [])
            self.assertEqual([row["review_status"] for row in saved["reviews"]], ["keep"])
            self.assertIsNone(read_fact(Path(saved["shared_store"]),
                                        (self.incident_id, "payments", self.environment_id)))

            # Applying later code creates a new run; restarting review must not
            # spend another model call on the old completed no-change decision.
            self.add_new_empty_run()
            repeated = memory.review_runs(self.root, {})
            self.assertEqual(repeated["facts"], 0)
            self.assertEqual(repeated["reviewed"], [])
            self.assertEqual(len(memory.memory_state(self.root)["reviews"]), 1)
            model.assert_called_once()
        with patch.object(memory, "load_implementation") as implementation:
            with self.assertRaisesRegex(ValueError, "published Memory fact"):
                memory.recall(self.root, {}, self.root / "agent.py", self.incident_id)
            implementation.assert_not_called()

    def test_proposed_is_counted_only_after_publication_and_remains_idempotent(self):
        with patch.object(memory, "review_facts", side_effect=self.background_decision("proposed")) as model:
            first = memory.review_runs(self.root, {})
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first["facts"], 1)
            self.assertEqual(first["published"], [self.incident_id])
            self.assertEqual(first["kept"], [])
            self.add_new_empty_run()
            repeated = memory.review_runs(self.root, {})
            self.assertEqual(repeated["facts"], 1)
            self.assertEqual(repeated["reviewed"], [])
            model.assert_called_once()
        saved = memory.load_json(self.root / "memory/state.json")
        self.assertEqual(len(saved["reviews"]), 1)
        self.assertEqual(saved["facts"][0]["review_status"], "proposed")
        self.assertEqual(read_fact(Path(saved["shared_store"]),
                                   (self.incident_id, "payments", self.environment_id)), self.fact)

    def test_unpublished_proposal_is_rejected_and_can_be_retried(self):
        with patch.object(memory, "review_facts", side_effect=self.background_decision("proposed", publish=False)):
            with self.assertRaisesRegex(ValueError, "was not published"):
                memory.review(self.root, self.run, {})
        self.assertEqual(memory.memory_state(self.root)["facts"], [])
        self.assertEqual(memory.memory_state(self.root)["reviews"], [])
        with patch.object(memory, "review_facts", side_effect=self.background_decision("proposed")) as model:
            result = memory.review(self.root, self.run, {})
            self.assertEqual(result["facts"], 1)
            model.assert_called_once()

    def test_legacy_keep_index_is_migrated_and_never_sent_to_the_model_again(self):
        legacy = memory.memory_state(self.root)
        entry = {**self.fact, "source_run": str(self.run), "review_status": "keep",
                 "review_path": str(self.root / "old-review")}
        legacy.update(status="ready", facts=[entry])
        (self.root / "memory").mkdir()
        memory.save_state(self.root, legacy)
        with patch.object(memory, "review_facts") as model:
            for _ in range(2):
                result = memory.review_runs(self.root, {})
                self.assertEqual(result["facts"], 0)
                self.assertEqual(result["status"], "empty")
                self.assertEqual(result["reviewed"], [])
            model.assert_not_called()
        saved = memory.load_json(self.root / "memory/state.json")
        self.assertEqual(saved["facts"], [])
        self.assertEqual(saved["reviews"], [entry])

    def test_missing_published_store_is_not_counted_and_does_not_block_repair(self):
        legacy = memory.memory_state(self.root)
        entry = {**self.fact, "source_run": str(self.run), "review_status": "proposed",
                 "review_path": str(self.root / "old-review")}
        legacy.update(status="ready", facts=[entry], reviews=[entry])
        (self.root / "memory").mkdir()
        memory.save_state(self.root, legacy)
        unavailable = memory.memory_state(self.root)
        self.assertEqual(unavailable["facts"], [])
        self.assertEqual(unavailable["status"], "empty")
        self.assertEqual(unavailable["reviews"], [entry])
        self.assertIn("unavailable", unavailable["last_error"])

        with patch.object(memory, "review_facts", side_effect=self.background_decision("proposed")) as model:
            repaired = memory.review_runs(self.root, {})
            self.assertEqual(repaired["facts"], 1)
            self.assertEqual(repaired["published"], [self.incident_id])
            model.assert_called_once()
        saved = memory.load_json(self.root / "memory/state.json")
        self.assertEqual(saved["status"], "ready")
        self.assertIsNone(saved["last_error"])

    def test_a_different_published_fact_cannot_satisfy_a_new_proposal(self):
        store = self.root / "memory" / "shared-facts.sqlite3"
        store.parent.mkdir()
        publish_fact(store, {**self.fact, "source_report_item_id": 1000010})
        with patch.object(memory, "review_facts", side_effect=self.background_decision("proposed", publish=False)):
            with self.assertRaisesRegex(ValueError, "was not published"):
                memory.review(self.root, self.run, {})
        self.assertEqual(memory.memory_state(self.root)["facts"], [])
        self.assertEqual(memory.memory_state(self.root)["reviews"], [])


if __name__ == "__main__":
    unittest.main()

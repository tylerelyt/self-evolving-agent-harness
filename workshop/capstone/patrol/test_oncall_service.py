"""Protect publication recovery and evidence-based adoption in the live service."""

from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


CAPSTONE = Path(__file__).resolve().parents[1]
if str(CAPSTONE) not in sys.path:
    sys.path.insert(0, str(CAPSTONE))

import oncall_service as service
from skillclaw_evolve import FORMAL_GROUP


class OncallServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "learning").mkdir()
        self.safe_sha = service.sha256(service.SEED)
        self.candidate = self.root / "candidate.md"
        self.candidate.write_text(service.SEED.read_text() + "\nControlled candidate.\n")
        self.candidate_sha = service.sha256(self.candidate)
        self.safe_source = self.root / "formal" / "versions" / self.safe_sha / service.ONCALL_SKILL / "SKILL.md"
        service.replace_skill(self.safe_source, service.SEED.read_bytes())
        for alias in service.ALIASES:
            service.replace_skill(self.receiver(alias), service.SEED.read_bytes())

    def receiver(self, alias):
        return self.root / "receivers" / alias / service.ONCALL_SKILL / "SKILL.md"

    def waiting_state(self):
        return {"status": "waiting", "active_sha256": self.safe_sha,
                "processed_sessions": [], "attempts": {}}

    def adoption_state(self):
        return {
            **self.waiting_state(), "active_sha256": self.candidate_sha,
            "last_release": {"sha256": self.candidate_sha, "previous_sha256": self.safe_sha,
                             "published_at": 100, "status": "awaiting_adoption", "adoptions": {}},
        }

    def completed(self, observed_at=110):
        return [
            {"instance": alias, "record": {
                "session_id": "session-" + alias, "incident_id": "incident-" + alias,
                "skill_sha256": self.candidate_sha, "skill_item_id": 10, "report_item_id": 30,
                "observation": json.dumps({"service": "payments", "environment_id": alias,
                                           "window_end": observed_at}),
            }}
            for alias in service.ALIASES
        ]

    def receipt(self, _state, record):
        scope = json.loads(record["observation"])
        return {
            "skill": {"success": True, "name": service.ONCALL_SKILL,
                      "sha256": self.candidate_sha, "content": self.candidate.read_text()},
            "probes": [
                {"service": scope["service"], "environment_id": scope["environment_id"],
                 "http_status": 503, "checked_at": 111, "call_item_id": 12, "item_id": 15},
                {"service": scope["service"], "environment_id": scope["environment_id"],
                 "http_status": 200, "checked_at": 112, "call_item_id": 18, "item_id": 20},
            ],
            "report": {"probe_http_status": 503, "probe_http_statuses": [503, 200],
                       "assessment": "intermittent"},
        }

    def mock_native_release(self, formal, source, digest, receivers):
        snapshot = formal / "versions" / digest / service.ONCALL_SKILL / "SKILL.md"
        service.replace_skill(snapshot, source.read_bytes())
        return {"sha256": digest}

    def test_new_sequential_evidence_confirms_adoption(self):
        state = self.adoption_state()
        with patch.object(service, "receipt", side_effect=self.receipt), patch.object(service, "publish") as publish:
            service.check_adoption(self.root, state, self.completed())
        self.assertEqual(state["last_release"]["status"], "adopted")
        self.assertEqual(set(state["last_release"]["adoptions"]), set(service.ALIASES))
        publish.assert_not_called()

    def test_observations_before_or_at_publication_cannot_confirm_adoption(self):
        for observed_at in (99, 100):
            with self.subTest(observed_at=observed_at):
                state = self.adoption_state()
                with patch.object(service, "receipt") as receipt, patch.object(service, "publish") as publish:
                    service.check_adoption(self.root, state, self.completed(observed_at))
                self.assertEqual(state["last_release"]["status"], "awaiting_adoption")
                self.assertEqual(state["last_release"]["adoptions"], {})
                receipt.assert_not_called()
                publish.assert_not_called()

    def check_invalid_adoption(self, change):
        def altered_receipt(state, record):
            proof = self.receipt(state, record)
            change(proof)
            return proof

        state = self.adoption_state()
        with patch.object(service, "receipt", side_effect=altered_receipt), patch.object(service, "publish") as publish:
            service.check_adoption(self.root, state, self.completed())
        self.assertEqual(state["last_release"]["status"], "rolled_back")
        self.assertEqual(state["active_sha256"], self.safe_sha)
        self.assertTrue(any(not entry["passed"] for entry in state["last_release"]["adoptions"].values()))
        publish.assert_called_once()

    def test_conclusion_must_match_probe_results(self):
        def change(proof):
            proof["report"]["assessment"] = "persistent"

        self.check_invalid_adoption(change)

    def test_second_probe_cannot_be_requested_before_first_result(self):
        def change(proof):
            proof["probes"][1]["call_item_id"] = proof["probes"][0]["call_item_id"]

        self.check_invalid_adoption(change)

    def test_loaded_content_must_match_approved_hash(self):
        def change(proof):
            proof["skill"]["content"] += " altered"

        self.check_invalid_adoption(change)

    def test_learning_restart_recovers_partial_publication_before_reading_incidents(self):
        state = self.waiting_state()
        replace = service.replace_skill

        def interrupt_second_receiver(path, content):
            if path == self.receiver("instance-b"):
                raise SystemExit("controlled interruption during delivery")
            return replace(path, content)

        with patch("skillclaw_evolve.release", side_effect=self.mock_native_release):
            with patch.object(service, "replace_skill", side_effect=interrupt_second_receiver):
                with self.assertRaises(SystemExit):
                    service.publish(self.root, self.candidate, self.candidate_sha, state=state)
            self.assertEqual(service.sha256(self.receiver("instance-a")), self.candidate_sha)
            self.assertEqual(service.sha256(self.receiver("instance-b")), self.safe_sha)

            def inspect_after_restart(_root):
                recovered = service.load_json(self.root / "learning" / "state.json")
                self.assertEqual(recovered["active_sha256"], self.safe_sha)
                self.assertNotIn("pending_release", recovered)
                self.assertEqual(recovered["publication_recovery"]["status"], "requires_reevaluation")
                for alias in service.ALIASES:
                    self.assertEqual(service.sha256(self.receiver(alias)), self.safe_sha)
                raise SystemExit("stop after first recovered learning iteration")

            with patch.object(service, "model_connection", return_value=nullcontext("http://127.0.0.1:1/v1")):
                with patch.object(service, "completed_incidents", side_effect=inspect_after_restart):
                    with self.assertRaises(SystemExit):
                        service.learn(self.root, False)

    def test_failed_recovery_retains_pending_publication_for_next_restart(self):
        state = self.waiting_state()
        with patch("skillclaw_evolve.release", side_effect=RuntimeError("controlled publication failure")):
            with self.assertRaises(RuntimeError):
                service.publish(self.root, self.candidate, self.candidate_sha, state=state)
        with patch.object(service, "publish", side_effect=RuntimeError("controlled recovery failure")):
            with self.assertRaises(RuntimeError):
                service.recover_publication(self.root, state)
        saved = service.load_json(self.root / "learning" / "state.json")
        self.assertEqual(saved["status"], "recovery_error")
        self.assertEqual(saved["active_sha256"], self.safe_sha)
        self.assertEqual(saved["pending_release"]["candidate_sha256"], self.candidate_sha)

    @unittest.skipUnless(importlib.util.find_spec("skillclaw"), "Install course SkillClaw dependencies for native publication")
    def test_native_interrupted_publication_restores_formal_and_both_receivers(self):
        service.publish(self.root, service.SEED, self.safe_sha)
        state = self.waiting_state()
        service.publish(self.root, self.candidate, self.candidate_sha, state=state)
        restarted = service.load_json(self.root / "learning" / "state.json")
        service.recover_publication(self.root, restarted)
        formal = self.root / "formal" / "shared" / FORMAL_GROUP / "skills" / service.ONCALL_SKILL / "SKILL.md"
        self.assertEqual(service.sha256(formal), self.safe_sha)
        for alias in service.ALIASES:
            self.assertEqual(service.sha256(self.receiver(alias)), self.safe_sha)
        self.assertEqual(restarted["publication_recovery"]["status"], "requires_reevaluation")
        self.assertNotIn("last_release", restarted)


if __name__ == "__main__":
    unittest.main()

"""Step boundaries, immutable evidence and scoped reset; no model requests."""
from copy import deepcopy
from contextlib import contextmanager
import json
from importlib.util import find_spec
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import steps
from test_run23 import scored_rows

NATIVE_HUB = find_spec("skillclaw") is not None
NATIVE_REASON = "Native SkillHub not installed; rerun with the pinned Hermes Python for actual publication/reset checks"


class StepBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = (Path(self.temp.name) / "exercise").resolve()
        steps.init(self.run)

    def state(self):
        return steps.native.read(self.run / "state.json")

    def prior(self, through):
        state = self.state()
        state["completed"] = {str(n): {"files": {}} for n in range(1, through + 1)}
        steps.write(self.run / "state.json", state)
        return state

    def publication_fixture(self):
        state = self.prior(4)
        baseline = self.run / "formal/timeout-triage/SKILL.md"
        steps.native.hub(self.run).push_skills(str(self.run / "formal"))
        state["formal_sha256"] = state["baseline_sha256"]
        body = baseline.read_text() + "\nCandidate for isolated gate unit tests.\n"
        staging = self.run / "candidate-source.md"
        staging.write_text(body)
        digest = steps.native.sha(staging)
        candidate = self.run / f"candidates/{digest}/timeout-triage/SKILL.md"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(body)
        record = {"source": "gepa", "candidate_id": digest, "base_version": state["baseline_sha256"],
                  "path": str(candidate.relative_to(self.run))}
        rows = scored_rows()
        for row in rows:
            for arm, actual in (("baseline", state["baseline_sha256"]), ("candidate", digest)):
                row[arm].pop("score")
                row[arm]["skill_sha256"] = actual
                row[arm]["trace"]["loaded_sha256"] = actual
        steps.write(self.run / "learning/generation.json", {"excluded_sessions": []})
        steps.write(steps.result_path(self.run, 3), {"replays": [{"candidate": record, "cases": rows}]})
        steps.write(steps.result_path(self.run, 4), {"decision": "REJECT", "raw_replay_sha256": steps.native.sha(steps.result_path(self.run, 3))})
        state["completed"]["2"]["files"] = steps.fingerprint(self.run, [record["path"]])
        state["completed"]["3"]["files"] = steps.fingerprint(self.run, [str(steps.result_path(self.run, 3).relative_to(self.run))])
        state["completed"]["4"]["files"] = steps.fingerprint(self.run, [str(steps.result_path(self.run, 4).relative_to(self.run))])
        steps.write(self.run / "state.json", state)
        steps.set_scoring(self.run)
        return candidate

    def test_init_is_a_visible_start_without_future_outputs_or_model_calls(self):
        self.assertEqual(self.state()["completed"], {})
        self.assertTrue((self.run / "formal/timeout-triage/SKILL.md").is_file())
        for name in ("online", "learning", "external-context", "shared", "receiver"):
            self.assertFalse((self.run / name).exists())

    def test_skipping_a_step_never_runs_prerequisites(self):
        with patch.object(steps, "preflight") as prepare, patch.object(steps.native, "execute_task") as task:
            with self.assertRaisesRegex(steps.StepError, "先完成第1步"):
                steps.execute(self.run, 3)
        prepare.assert_not_called()
        task.assert_not_called()

    def test_hash_change_is_rejected_before_any_native_action(self):
        state = self.prior(1)
        path = self.run / "external-context/external-context.json"
        steps.write(path, {"content": "fixed"})
        state["completed"]["1"]["files"] = steps.fingerprint(self.run, [str(path.relative_to(self.run))])
        steps.write(self.run / "state.json", state)
        steps.write(path, {"content": "changed"})
        with patch.object(steps, "preflight") as prepare:
            with self.assertRaisesRegex(steps.StepError, "证据已改变"):
                steps.execute(self.run, 2)
        prepare.assert_not_called()

    def test_parent_named_cache_does_not_disable_evidence_hashes(self):
        run = (Path(self.temp.name) / "cache/exercise").resolve()
        steps.init(run)
        steps.write(run / "evidence.json", {"source": "must be protected"})
        self.assertEqual(set(steps.fingerprint(run, ["evidence.json"])), {"evidence.json"})

    def test_state_is_reread_after_lock_acquisition(self):
        @contextmanager
        def another_command_finished(run):
            state = self.state()
            steps.write(steps.result_path(run, 1), {"already": "completed by A"})
            state["completed"]["1"] = {"files": steps.fingerprint(run, [str(steps.result_path(run, 1).relative_to(run))])}
            steps.write(run / "state.json", state)
            yield
        with patch.object(steps, "locked", another_command_finished), patch.object(steps, "step1") as action:
            self.assertEqual(steps.execute(self.run, 1)["status"], "already_complete")
        action.assert_not_called()

    def test_completed_step_is_cached_without_new_native_calls(self):
        def completed(run, state):
            return {"observed": "unit fixture, not a model run"}
        with patch.object(steps, "preflight") as prepare, patch.object(steps, "step1", side_effect=completed) as action:
            self.assertEqual(steps.execute(self.run, 1)["status"], "complete")
            self.assertEqual(steps.execute(self.run, 1)["status"], "already_complete")
        self.assertEqual(action.call_count, 1)
        self.assertEqual(prepare.call_count, 1)

    def test_step5_control_prompt_does_not_mark_the_step_failed(self):
        self.prior(4)
        with patch.object(steps, "preflight") as prepare:
            with self.assertRaisesRegex(steps.StepError, "scoring --available"):
                steps.execute(self.run, 5)
        self.assertIsNone(self.state()["failed"])
        prepare.assert_not_called()

    def test_scoring_is_the_only_mutable_input_and_keeps_prior_rejection(self):
        state = self.prior(4)
        steps.write(steps.result_path(self.run, 4), {"decision": "REJECT"})
        state["completed"]["4"]["files"] = steps.fingerprint(self.run, [str(steps.result_path(self.run, 4).relative_to(self.run))])
        steps.write(self.run / "state.json", state)
        before = steps.result_path(self.run, 4).read_bytes()
        steps.set_scoring(self.run)
        steps.verify(self.run, self.state())
        self.assertFalse(steps.controls(self.run)["score_outage"])
        self.assertEqual(before, steps.result_path(self.run, 4).read_bytes())

    @unittest.skipUnless(NATIVE_HUB, NATIVE_REASON)
    def test_regrading_publishes_same_trace_without_model_and_repeat_does_not_republish(self):
        candidate = self.publication_fixture()
        before = steps.result_path(self.run, 4).read_bytes()
        with patch.object(steps, "preflight"), patch.object(steps.native, "execute_task", side_effect=AssertionError("step5 must not call a model")):
            steps.execute(self.run, 5)
            version = steps.native.hub(self.run).list_remote()[0]["version"]
            self.assertEqual(steps.execute(self.run, 5)["status"], "already_complete")
        self.assertEqual(steps.native.hub(self.run).list_remote()[0]["version"], version)
        self.assertEqual(steps.native.sha(steps.shared_file(self.run)), steps.native.sha(candidate))
        self.assertEqual(before, steps.result_path(self.run, 4).read_bytes())
        self.assertEqual(steps.native.read(steps.result_path(self.run, 5))["model_tasks_executed"], 0)

    @unittest.skipUnless(NATIVE_HUB, NATIVE_REASON)
    def test_candidate_edit_cannot_be_evaluated_with_old_trace(self):
        candidate = self.publication_fixture()
        candidate.write_text(candidate.read_text() + "changed after replay\n")
        with patch.object(steps, "preflight") as prepare:
            with self.assertRaisesRegex(steps.StepError, "证据已改变"):
                steps.execute(self.run, 5)
        prepare.assert_not_called()

    @unittest.skipUnless(NATIVE_HUB, NATIVE_REASON)
    def test_reset3_restores_shared_baseline_and_keeps_candidates(self):
        candidate = self.publication_fixture()
        with patch.object(steps, "preflight"):
            steps.execute(self.run, 5)
        before = candidate.read_bytes()
        reset = steps.reset(self.run, 3)
        self.assertEqual(self.state()["formal_sha256"], self.state()["baseline_sha256"])
        self.assertEqual(steps.native.sha(steps.shared_file(self.run)), self.state()["baseline_sha256"])
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(set(self.state()["completed"]), {"1", "2"})
        self.assertTrue((Path(reset["archive"]) / "steps/05-publish.json").is_file())

    @unittest.skipUnless(NATIVE_HUB, NATIVE_REASON)
    def test_reset6_preserves_published_version_and_prior_source(self):
        candidate = self.publication_fixture()
        with patch.object(steps, "preflight"):
            steps.execute(self.run, 5)
        old_sha = self.state()["formal_sha256"]
        a, c = self.run / "experiences/lesson23-a.json", self.run / "experiences/lesson23-c.json"
        steps.write(a, {"source": "A"})
        steps.write(c, {"source": "C"})
        steps.write(self.run / "next-experience.json", {"session_id": "lesson23-c"})
        steps.write(steps.result_path(self.run, 6), {"receiver": "C"})
        state = self.state()
        state["completed"]["6"] = {"files": steps.fingerprint(self.run, steps.owned_paths(self.run, 6))}
        steps.write(self.run / "state.json", state)
        steps.reset(self.run, 6)
        self.assertEqual(self.state()["formal_sha256"], old_sha)
        self.assertEqual(steps.native.sha(steps.shared_file(self.run)), steps.native.sha(candidate))
        self.assertTrue(a.exists())
        self.assertFalse(c.exists())

    def test_reset_rejects_external_symlinks_before_hub_write(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("do not touch")
        (self.run / "learning").mkdir()
        (self.run / "learning/replay").symlink_to(outside, target_is_directory=True)
        with patch.object(steps.native, "hub") as hub:
            with self.assertRaises(steps.StepError):
                steps.reset(self.run, 3)
        hub.assert_not_called()
        self.assertEqual(sentinel.read_text(), "do not touch")

    def test_steps_and_archive_symlinks_cannot_receive_writes(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.run / "steps").symlink_to(outside, target_is_directory=True)
        with patch.object(steps, "preflight") as prepare:
            with self.assertRaises(steps.StepError):
                steps.execute(self.run, 1)
        prepare.assert_not_called()
        (self.run / "steps").unlink()
        (self.run / "archive").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(steps.StepError):
            steps.reset(self.run, 6)
        self.assertEqual(list(outside.iterdir()), [])

    def test_broken_source_json_can_be_archived_by_reset(self):
        (self.run / "experience.json").write_text("{")
        reset = steps.reset(self.run, 1)
        self.assertEqual((Path(reset["archive"]) / "experience.json").read_text(), "{")
        self.assertFalse((self.run / "experience.json").exists())

    def test_failed_step_retains_evidence_until_explicit_reset(self):
        with patch.object(steps, "preflight"), patch.object(steps, "step1", side_effect=RuntimeError("fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                steps.execute(self.run, 1)
        with patch.object(steps, "preflight") as prepare:
            with self.assertRaisesRegex(steps.StepError, "reset --from-step 1"):
                steps.execute(self.run, 1)
        prepare.assert_not_called()
        steps.reset(self.run, 1)
        self.assertIsNone(self.state()["failed"])

    def test_reference_and_wrong_group_markers_are_rejected(self):
        manifest = steps.native.read(self.run / "run.json")
        manifest["group_id"] = "lesson23-exercise-another-run"
        steps.write(self.run / "run.json", manifest)
        with self.assertRaisesRegex(steps.StepError, "不是第23讲"):
            steps.open_state(self.run)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Publication invariants and native-receipt checks, with no model requests."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import run23
from scenario import CASES, SCOPE, SKILL, TOOLS, decide, report_for, score, snapshots, validate_task


def scored_rows():
    return [{"case": case, "baseline": {"session_id": "baseline-" + case, "skill_sha256": "a",
                "trace": {"valid_trace": True, "loaded_sha256": "a"}, "score": score({"valid_trace": True, "passed": case == "upstream"})},
             "candidate": {"session_id": "candidate-" + case, "skill_sha256": "b",
                "trace": {"valid_trace": True, "loaded_sha256": "b"}, "score": score({"valid_trace": True, "passed": True})}}
            for case in CASES]


def native_sample(case="drift"):
    skill = "---\nname: timeout-triage\n---\nFixture only.\n"
    digest = hashlib.sha256(skill.encode()).hexdigest()
    calls, messages, receipts = [], [], {}
    plan = ["skill_view", *TOOLS] if case == "drift" else ["skill_view", *TOOLS[:2]]
    for index, name in enumerate(plan):
        args = {"name": SKILL} if name == "skill_view" else dict(SCOPE)
        value = {"success": True, "content": skill} if name == "skill_view" else snapshots(case)[name]
        messages.extend([{"role": "assistant", "tool_calls": [{"id": str(index), "function": {"name": name, "arguments": json.dumps(args)}}]},
                         {"role": "tool", "tool_call_id": str(index), "name": name, "content": json.dumps(value)}])
        if name in TOOLS:
            calls.append({"tool": name, "arguments": args, "result": value})
            receipts[name] = value
    messages.append({"role": "assistant", "content": json.dumps(report_for(receipts))})
    return messages, calls, digest


class CandidateGateTests(unittest.TestCase):
    def test_score_error_is_unjudgeable_and_cannot_be_averaged_into_approval(self):
        rows = scored_rows()
        rows[1]["candidate"]["score"] = score({"valid_trace": True, "passed": True}, unavailable=True)
        self.assertIsNone(rows[1]["candidate"]["score"]["passed"])
        self.assertEqual(decide(rows, "a", "b"), {"decision": "REJECT", "reason": "unjudgeable_cases"})

    def test_missing_cases_and_execution_errors_block_publication(self):
        rows = scored_rows()
        self.assertEqual(decide(rows[:1], "a", "b")["reason"], "incomplete_cases")
        rows[0]["baseline"]["score"] = {"status": "execution_error", "passed": None}
        self.assertEqual(decide(rows, "a", "b")["decision"], "REJECT")

    def test_verified_gain_is_required_without_regressions(self):
        rows = scored_rows()
        self.assertEqual(decide(rows, "a", "b")["decision"], "APPROVE")
        self.assertEqual(decide(rows, "a", "a")["decision"], "REJECT")
        rows[1]["candidate"]["score"] = score({"valid_trace": True, "passed": False})
        self.assertEqual(decide(rows, "a", "b")["decision"], "REJECT")

    def test_invalid_hash_reused_session_or_training_session_cannot_be_published(self):
        for mutation in ("hash", "trace", "shared", "training"):
            rows = scored_rows()
            if mutation == "hash":
                rows[0]["candidate"]["skill_sha256"] = "unexpected"
            elif mutation == "trace":
                rows[0]["candidate"]["trace"]["valid_trace"] = False
            elif mutation == "shared":
                rows[0]["candidate"]["session_id"] = rows[0]["baseline"]["session_id"]
            with self.subTest(mutation=mutation):
                sources = [rows[0]["candidate"]["session_id"]] if mutation == "training" else []
                self.assertEqual(decide(rows, "a", "b", source_sessions=sources)["decision"], "REJECT")

    def test_registration_preserves_baseline_and_records_generator_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "baseline.md"
            candidate = root / "candidate.md"
            baseline.write_text("---\nname: timeout-triage\n---\nOld method.\n")
            candidate.write_text(baseline.read_text() + "New method.\n")
            before = run23.sha(baseline)
            record = run23.register_candidate(root, candidate, baseline,
                {"session_id": "source", "archive_sha256": "a" * 64})
            self.assertEqual((record["status"], record["base_version"]), ("pending_evaluation", before))
            self.assertEqual(run23.sha(baseline), before)
            with self.assertRaisesRegex(ValueError, "GEPA handoff"):
                run23.register_candidate(root, candidate, baseline, {}, generator="gepa")
            candidate.write_text(candidate.read_text() + "Alternative candidate.\n")
            gepa = run23.register_candidate(root, candidate, baseline,
                {"dataset_sha256": "b" * 64, "optimizer_run": "own-lesson23-run"}, generator="gepa")
            self.assertEqual(gepa["source"], "gepa")
            self.assertEqual(gepa["dataset_sha256"], "b" * 64)
            self.assertFalse((root / "shared").exists())


class TraceTests(unittest.TestCase):
    def test_ordered_scoped_receipts_support_each_frozen_case(self):
        for case in CASES:
            with self.subTest(case=case):
                messages, calls, digest = native_sample(case)
                self.assertTrue(validate_task(messages, calls, case, digest)["passed"])

    def test_wrong_skill_scope_or_invented_report_is_rejected(self):
        messages, calls, digest = native_sample()
        with self.assertRaisesRegex(ValueError, "pinned Skill"):
            validate_task(messages, calls, "drift", "0" * 64)
        changed = deepcopy(calls)
        changed[0]["arguments"]["environment"] = "elsewhere"
        with self.assertRaises(ValueError):
            validate_task(messages, changed, "drift", digest)
        messages[-1]["content"] = '{"assessment":"root_cause_confirmed"}'
        with self.assertRaisesRegex(ValueError, "Final report"):
            validate_task(messages, calls, "drift", digest)

    def test_same_decision_batch_cannot_prove_a_conditional_check(self):
        messages, calls, digest = native_sample()
        config_call = messages[6]["tool_calls"][0]
        config_receipt = messages[7]
        messages[2]["tool_calls"].append(config_call)
        messages.insert(4, config_receipt)
        del messages[7:9]
        self.assertFalse(validate_task(messages, calls, "drift", digest)["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Bind engineer guidance to branch receipts and the model's actual revision input."""

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CAPSTONE = Path(__file__).resolve().parents[1]
if str(CAPSTONE) not in sys.path:
    sys.path.insert(0, str(CAPSTONE))

from engineering_analysis import build_analysis, load_analysis
from environment import load_json, write_json
from skillclaw_evolve import ENGINEERING_MARKER, _scripted_reply


class EngineeringAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.content = "Pinned initial Skill."
        self.skill_sha = sha256(self.content.encode()).hexdigest()
        self.sources = []
        for alias in ("instance-a", "instance-b"):
            branch = self.root / f"{alias}.json"
            write_json(branch, {"session_id": "fork-" + alias, "report_item_id": 22, "skill_item_id": 12,
                                "probes": [{"http_status": status} for status in (503, 200, 503)],
                                "report": {"assessment": "intermittent", "root_cause": "unknown"}})
            self.sources.append({"instance": alias, "worker": self.root / alias,
                                 "record": {"session_id": "initial-" + alias, "incident_id": "incident-" + alias,
                                            "report_item_id": 10, "skill_sha256": self.skill_sha}, "branch_path": branch})
        verifier = patch("workbench_memory.verified_branch", side_effect=lambda worker, record, branch: {
            "incident_id": record["incident_id"], "service": "payments", "environment_id": worker.name,
        })
        initial = patch("fleet.receipt", return_value={
            "skill": {"content": self.content}, "probes": [{"http_status": 503}],
        })
        self.verifier = verifier.start()
        initial.start()
        self.addCleanup(verifier.stop)
        self.addCleanup(initial.stop)

    def test_artifact_binds_policy_source_branch_hashes_and_original_sessions(self):
        reference = build_analysis(self.root / "analysis", self.sources)
        ids = {source["record"]["session_id"] for source in self.sources}
        guidance = load_analysis(reference, self.skill_sha, ids)
        self.assertEqual(self.verifier.call_count, 4)
        self.assertEqual(guidance["policy"]["author"], "course-scripted-engineer")
        for case in guidance["cases"]:
            self.assertEqual(case["followup_probe_http_statuses"], [503, 200, 503])
            self.assertEqual(case["assessment"], "intermittent")
            self.assertEqual(case["method_recommendation"]["action"], "add_bounded_second_probe")
            self.assertNotIn(case["branch_session_id"], ids)
        with self.assertRaisesRegex(ValueError, "initial-only"):
            load_analysis(reference, self.skill_sha, {"fork-instance-a", "fork-instance-b"})
        Path(reference["path"]).write_text("{}")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            load_analysis(reference, self.skill_sha, ids)

    def test_changed_branch_receipts_cannot_reuse_an_old_analysis(self):
        reference = build_analysis(self.root / "analysis", self.sources)
        path = self.sources[0]["branch_path"]
        branch = load_json(path)
        branch["probes"][0]["http_status"] = 200
        write_json(path, branch)
        with self.assertRaisesRegex(ValueError, "differs from its verified evidence"):
            load_analysis(reference, self.skill_sha, {"initial-instance-a", "initial-instance-b"})

    def test_scripted_revision_consumes_the_bounded_engineer_recommendation(self):
        ids = {"initial-a", "initial-b"}
        user = '\n'.join(f'### Session {sid}\noncall_probe({{"http_status": 503}})' for sid in sorted(ids))
        messages = [{"role": "system", "content": "edit the ORIGINAL skill"}, {"role": "user", "content": user}]
        baseline = {"name": "oncall-alert-triage", "content": self.content}
        for action, expected in (("add_bounded_second_probe", "improve_skill"),
                                 ("retain_existing_probe_rule", "skip")):
            guidance = {"cases": [{"source_session_id": sid, "method_recommendation": {"action": action}} for sid in ids]}
            request = [messages[0], {**messages[1], "content": user + ENGINEERING_MARKER + json.dumps(guidance)}]
            response = json.loads(_scripted_reply(request, baseline, ids))
            self.assertEqual(response["action"], expected)


if __name__ == "__main__":
    unittest.main()

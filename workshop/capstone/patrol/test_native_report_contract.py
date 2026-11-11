"""Keep native archives and bounded Hermes turns compatible with live reports."""

import hashlib
from importlib.util import find_spec
import json
from pathlib import Path
import sys
import tempfile
import unittest

from hermes_runtime import SOURCE, open_sessions, run_turn
from skillclaw_bridge import incident_archive


class NativeArchiveReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = "patrol-test-report"
        self.incident = "lab-payments-0123456789ab"
        self.skill = "Approved On-call Skill content for this archive test.\n"
        self.digest = hashlib.sha256(self.skill.encode()).hexdigest()
        self.report = {"incident_id": self.incident, "probe_http_status": 503,
                       "probe_http_statuses": [503], "assessment": "failure_observed"}
        receipts = {
            "service_snapshot": {"service": "payments", "environment_id": "archive-test", "error_rate": .5},
            "skill_view": {"name": "oncall-alert-triage", "content": self.skill, "sha256": self.digest},
            "oncall_probe": {"service": "payments", "environment_id": "archive-test", "http_status": 503},
        }
        calls = [{"id": name, "function": {"name": name, "arguments": "{}"}} for name in receipts]
        results = [{"tool_call_id": name, "name": name, "content": json.dumps(value)}
                   for name, value in receipts.items()]
        self.record = {"session_id": self.session, "user_alias": "instance-a", "num_turns": 2,
                       "turns": [{"prompt_text": "test-patrol", "tool_calls": calls,
                                  "tool_results_raw": results, "response_text": "读取现场。"},
                                 {"prompt_text": "test-patrol", "tool_calls": [], "tool_results_raw": [],
                                  "response_text": json.dumps(self.report)}]}
        self.path = self.root / "shared" / "test-group" / "sessions" / (self.session + ".json")
        self.path.parent.mkdir(parents=True)

    def select(self, response):
        self.record["turns"][-1]["response_text"] = response
        self.path.write_text(json.dumps(self.record), encoding="utf-8")
        original = self.path.read_bytes()
        result = incident_archive(self.root, "instance-a", self.session, group_id="test-group",
                                  incident_id=self.incident, skill_sha256=self.digest)
        self.assertEqual(self.path.read_bytes(), original)
        return result

    def test_archive_accepts_the_same_unambiguous_model_wrappers_as_session_validation(self):
        body = json.dumps(self.report, ensure_ascii=False)
        for reply in (body, "调查完成。\n```json\n" + body + "\n```\n依据来自本轮探测。",
                      "本轮初判如下。\n\n" + body):
            with self.subTest(reply=reply):
                self.assertEqual(self.select(reply), self.path)

    def test_archive_rejects_multiple_report_candidates(self):
        body = json.dumps(self.report)
        for reply in ("```json\n" + body + "\n```\n```json\n" + body + "\n```",
                      "第一份\n" + body + "\n第二份\n" + body):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                self.select(reply)

    def test_presentation_normalization_does_not_relax_probe_or_incident_checks(self):
        for change in ({"probe_http_status": 200}, {"probe_http_statuses": [200]},
                       {"incident_id": "lab-payments-000000000000"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.select("初判完成。\n" + json.dumps({**self.report, **change}))


@unittest.skipUnless((SOURCE / "run_agent.py").is_file() and find_spec("dotenv") is not None,
                     "Run native turn checks with the pinned Hermes environment and site packages")
class NativeReportBudgetTests(unittest.TestCase):
    def test_a_report_with_seven_real_tool_steps_completes_within_the_bounded_default(self):
        # A model may spend more than six iterations reading a Skill, gathering
        # sequential probes and checking evidence before returning its report.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from local_model import serve

        def respond(messages):
            latest = max(index for index, row in enumerate(messages) if row["role"] == "user")
            count = sum(row["role"] == "tool" for row in messages[latest:])
            if count < 7:
                return "search_inspections", {}
            return None, {"status": "complete", "tool_receipts": count}

        with tempfile.TemporaryDirectory() as directory, serve(respond) as url:
            state = Path(directory)
            prompt = 'HANDOFF_SCOPE {"service":"payments","environment_id":"bounded-test"}\n只检查指定范围。'
            with open_sessions(state) as sessions:
                rows, _ = run_turn(sessions, "native-budget-test", prompt,
                                   {"search_inspections": lambda: []},
                                   {"base_url": url, "api_key": "local-fixture", "model": "report-budget-test"})
            self.assertEqual(sum(row["role"] == "tool" for row in rows), 7)
            self.assertEqual(json.loads(rows[-1]["content"]), {"status": "complete", "tool_receipts": 7})
            self.assertEqual([row["content"] for row in rows if row["role"] == "user"], [prompt])


if __name__ == "__main__":
    unittest.main()

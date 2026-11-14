"""The front-door command preserves failure, evidence and model-mode boundaries."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import demo


class DemoTests(unittest.TestCase):
    def test_startup_failure_does_not_claim_to_have_saved_an_output(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(demo.subprocess, "run", return_value=SimpleNamespace(returncode=5)), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            output = Path(directory) / "not-created"
            self.assertEqual(demo.main(["--out", str(output)]), 5)
            self.assertIn("Output was not created", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_failure_propagates_without_claiming_success_or_removing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            def failed(command, **kwargs):
                output.mkdir()
                (output / "failure.txt").write_text("incomplete publication")
                self.assertNotIn("--live", command)
                self.assertIn("--verify-closed-loop", command)
                self.assertIn("--rehearse-rollback", command)
                return SimpleNamespace(returncode=7)
            with patch.object(demo.subprocess, "run", side_effect=failed), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                self.assertEqual(demo.main(["--out", str(output)]), 7)
            self.assertEqual((output / "failure.txt").read_text(), "incomplete publication")
            self.assertNotIn("verified results", stdout.getvalue())

    def test_existing_output_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(demo.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                demo.main(["--out", directory])
            run.assert_not_called()

    def test_summary_reads_current_results_and_rejects_unverified_or_live_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            report = {"mode": "local-script-model", "closed_loop_verified": True,
                "rounds": [{"decision": "publish", "evaluation": {"cases": [
                    {"baseline": {"passed": False}, "candidate": {"passed": True}}]}},
                    {"adoption_verified": True, "patrol_evidence": "proof.json"}],
                "rollback_rehearsal": {"status": "completed"}}
            (output / "proof.json").write_text(json.dumps({"instance-a": {"probes": [{"http_status": 503}, {"http_status": 200}]}}))
            (output / "report.json").write_text(json.dumps(report))
            with redirect_stdout(io.StringIO()) as stdout:
                demo.print_results(output)
            self.assertIn("baseline 0/1 -> candidate 1/1", stdout.getvalue())
            self.assertIn("instance-a: [503, 200]", stdout.getvalue())
            for field, value in (("mode", "live"), ("closed_loop_verified", False)):
                changed = {**report, field: value}
                (output / "report.json").write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    demo.print_results(output)


if __name__ == "__main__":
    unittest.main()

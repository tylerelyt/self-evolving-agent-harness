"""Bad model behavior rejects a method without hiding transport failures."""

from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall.paths import prepare
prepare()
import recovery_skill_task as task
from audit_fleet import audit_recovery_case


class QualityRejectionTests(unittest.TestCase):
    def test_completed_turn_without_required_tools_returns_a_rejected_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("A proposed recovery method.")
            digest = sha256(skill.read_bytes()).hexdigest()
            case_id, evidence, expected = next(task.gate_cases())

            def no_tools(_sessions, _session, prompt, *_args, **_kwargs):
                return [{"id": 1, "role": "user", "content": prompt},
                        {"id": 2, "role": "assistant", "content": '{"decision":"recovered"}'}], []

            with patch.object(task, "open_sessions", return_value=nullcontext(object())), \
                 patch.object(task, "install_oncall_skill", return_value=digest), \
                 patch.object(task, "run_turn", side_effect=no_tools):
                result = task.run_case(root / "case", skill, digest, case_id, evidence, expected, {})
            self.assertFalse(result["valid_trace"])
            self.assertFalse(result["passed"])
            self.assertIsNone(result["report"])
            self.assertIn("ordered Skill read", result["error"])
            self.assertEqual(skill.read_text(), "A proposed recovery method.")

    def test_independent_audit_cannot_treat_an_invalid_trace_as_a_pass(self):
        _, evidence, expected = next(task.gate_cases())
        rows = [{"id": 1, "role": "user", "content": "assigned scope"},
                {"id": 2, "role": "assistant", "content": '{"decision":"recovered"}'}]
        try:
            task.read_evidence(rows, "assigned scope", evidence, "a" * 64)
        except RuntimeError as error:
            message = str(error)
        case = {"passed": False, "valid_trace": False, "report": None,
                "error": message, "expected_decision": expected}
        self.assertFalse(audit_recovery_case(rows, "assigned scope", evidence, "a" * 64, case, expected))
        case["passed"] = True
        with self.assertRaises(ValueError):
            audit_recovery_case(rows, "assigned scope", evidence, "a" * 64, case, expected)

    def test_provider_failure_remains_an_execution_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_id, evidence, expected = next(task.gate_cases())
            with patch.object(task, "open_sessions", return_value=nullcontext(object())), \
                 patch.object(task, "install_oncall_skill", return_value="a" * 64), \
                 patch.object(task, "run_turn", side_effect=RuntimeError("Provider unavailable")), \
                 self.assertRaisesRegex(RuntimeError, "Provider unavailable"):
                task.run_case(root / "case", root / "SKILL.md", "a" * 64, case_id, evidence, expected, {})


if __name__ == "__main__":
    unittest.main()

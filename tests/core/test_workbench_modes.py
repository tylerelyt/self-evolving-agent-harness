"""A maintained workbench runs the system; tutorial guidance is opt-in."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import sys
import tempfile
from unittest.mock import Mock, patch
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall.paths import prepare
prepare()
import app
from workbench import Workbench


class WorkbenchModeTests(unittest.TestCase):
    def test_constructor_compatibility_and_system_snapshot_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(Workbench(Path(temporary) / "tutorial", ROOT / "apps/oncall/workflow.py").mode, "tutorial")
            system = Workbench(Path(temporary) / "system", ROOT / "apps/oncall/workflow.py", mode="system")
            with patch("workbench.stage_status", side_effect=AssertionError("teaching readiness must not govern the system")):
                snapshot = system.snapshot()
            self.assertEqual(snapshot["mode"], "system")
            self.assertEqual(snapshot["agent"]["stages"], [])
            self.assertEqual(snapshot["checks"], [])
            with self.assertRaisesRegex(ValueError, "diagnostics.*reference acceptance"):
                system.action({"action": "check", "stage": 1})
            with self.assertRaisesRegex(ValueError, "mode"):
                Workbench(Path(temporary) / "bad", ROOT / "apps/oncall/workflow.py", mode="invalid")

    def test_system_start_does_not_follow_partial_tutorial_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            system = Workbench(temporary, ROOT / "apps/oncall/workflow.py", mode="system")
            child = Mock()
            child.poll.return_value = None
            with patch("workbench.stage_status", side_effect=AssertionError("unexpected teaching path")), \
                 patch.object(system, "acknowledge_recovered_alerts", return_value={"acknowledged": [], "errors": []}), \
                 patch.object(system, "spawn", return_value=child) as spawn:
                system.start_agent()
            command = spawn.call_args.args[0]
            self.assertNotIn("--patrol-only", command)
            self.assertNotIn("--no-learning", command)
            self.assertNotIn("阶段", system.record["events"][-1]["message"])

    def test_canonical_app_defaults_to_system_and_explicit_tutorial_is_available(self):
        for supplied, expected in (([], "system"), (["--tutorial"], "tutorial")):
            with self.subTest(mode=expected), tempfile.TemporaryDirectory() as temporary, \
                 patch.object(sys, "argv", ["oncall-workbench", "--offline", "--state", temporary, *supplied]), \
                 patch.object(app, "Workbench") as factory, patch.object(app, "make_server") as server_factory, \
                 patch.object(app.signal, "signal"), redirect_stdout(StringIO()):
                app.main()
                self.assertEqual(factory.call_args.kwargs["mode"], expected)
                server_factory.return_value.serve_forever.assert_called_once()

    def test_legacy_workshop_entry_defaults_to_tutorial(self):
        entry = ROOT / "workshop/capstone/app.py"
        with patch.object(sys, "argv", [str(entry), "--offline"]), \
             patch("apps.oncall.legacy.execute") as execute:
            runpy.run_path(str(entry), run_name="__main__")
            self.assertEqual(sys.argv[sys.argv.index("--mode") + 1], "tutorial")
            self.assertIn("lab/self_improvement_agent.py", sys.argv[sys.argv.index("--implementation") + 1])
            execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()

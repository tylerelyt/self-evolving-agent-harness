"""Real local process lifecycle checks without model or messaging credentials."""

from contextlib import contextmanager
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from self_evolving_harness.deployment import doctor, owned_profile, process_environment, read_profile, status


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "profile.json"
        self.spec = {"schema_version": 1, "name": "personal", "purpose": "personal", "state_dir": "state",
                     "command": ["{python}", "-c", "import time;time.sleep(60)"],
                     "doctor_command": ["{python}", "-c", "print('runtime available')"],
                     "poll_seconds": .05, "stop_timeout": 2}

    def profile(self, **changes):
        self.path.write_text(json.dumps({**self.spec, **changes}))
        return read_profile(self.path, repo=ROOT)

    def test_state_paths_and_environment_are_owned_by_the_profile(self):
        first = self.profile()
        second = self.profile(name="dedicated", purpose="dedicated", state_dir="other-state")
        self.assertNotEqual(process_environment(first)["HERMES_HOME"], process_environment(second)["HERMES_HOME"])
        self.assertEqual(process_environment(first)["RSI_PROFILE"], "personal")
        for environment in ({"HOME": "/tmp/other"}, {"HERMES_HOME": "/tmp/other"}, {"API_KEY": "do-not-store"}):
            with self.subTest(environment=list(environment)), self.assertRaises(ValueError):
                self.profile(environment=environment)

    def test_validation_rejects_ambiguous_commands_and_invalid_intervals(self):
        for changes in ({"command": "echo unsafe"}, {"name": "../other"}, {"poll_seconds": float('nan')},
                        {"stop_timeout": float('inf')}, {"state_dir": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.profile(**changes)

    def test_doctor_checks_runtime_without_claiming_credential_readiness(self):
        result = doctor(self.profile())
        self.assertTrue(result["runtime_check_passed"])
        self.assertFalse(result["credentials_verified"])
        self.assertIn("runtime available", Path(result["log"]).read_text())

    def test_process_workspace_has_only_one_effective_owner(self):
        profile = self.profile()
        with owned_profile(profile):
            self.assertTrue(status(profile)["owner_active"])
            with self.assertRaisesRegex(RuntimeError, "owner"):
                with owned_profile(profile):
                    self.fail("duplicate owner entered")
        self.assertFalse(status(profile)["owner_active"])

    def test_a_different_profile_cannot_reuse_an_existing_workspace(self):
        first = self.profile()
        with owned_profile(first):
            pass
        second = self.profile(name="enterprise", purpose="dedicated")
        with self.assertRaisesRegex(ValueError, "different deployment profile"):
            with owned_profile(second):
                self.fail("mixed deployment identities")

    @contextmanager
    def launched(self, profile):
        output = (self.root / "supervisor.log").open("w")
        child = subprocess.Popen([sys.executable, "-B", str(ROOT / "deploy/manage.py"), "launch",
                                  "--profile", str(self.path)], stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                path = profile["state"] / "deployment.json"
                if path.exists():
                    observed = json.loads(path.read_text())
                    if observed["status"] == "running" and observed["supervisor_pid"] == child.pid:
                        break
                if child.poll() is not None:
                    self.fail((self.root / "supervisor.log").read_text())
                time.sleep(.05)
            else:
                self.fail("supervisor did not start")
            yield child
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
                    self.fail("supervisor did not stop")
            output.close()

    def test_sigterm_stops_the_runtime_and_retains_its_evidence(self):
        profile = self.profile()
        with self.launched(profile) as child:
            self.assertTrue(status(profile)["owner_active"])
        saved = status(profile)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(saved["owner_active"])
        self.assertEqual(saved["last_observed"]["status"], "stopped")
        self.assertTrue(list((profile["state"] / "logs").glob("runtime-*.log")))

    def test_restart_has_a_fresh_owner_and_preserves_both_launch_records(self):
        profile = self.profile()
        runs = []
        for _ in range(2):
            with self.launched(profile) as child:
                saved = status(profile)["last_observed"]
                self.assertEqual(saved["supervisor_pid"], child.pid)
                runs.append(saved["run_id"])
        self.assertNotEqual(runs[0], runs[1])
        self.assertEqual(len(list((profile["state"] / "deployments").glob("*.json"))), 2)

    def test_adapter_command_is_an_argument_array_without_shell_expansion(self):
        marker = self.root / "should-not-exist"
        payload = f"$(touch {marker})"
        profile = self.profile(doctor_command=["{python}", "-c", "import sys;print(sys.argv[1])", payload])
        result = doctor(profile)
        self.assertIn(payload, Path(result["log"]).read_text())
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

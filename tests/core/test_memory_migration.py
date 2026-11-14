"""Enable memory as an explicit additive migration of a live service binding."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("RSI_NATIVE_MEMORY_ENDPOINT"), "opt-in actual OpenViking endpoint required")
class MemoryMigrationTests(unittest.TestCase):
    def test_explicit_enable_preserves_service_sessions_and_environment_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "run"
            template = root / "memory.json"
            template.write_text(json.dumps({"schema_version": 1, "backend": "openviking",
                "endpoint": os.environ["RSI_NATIVE_MEMORY_ENDPOINT"], "state_dir": "authority", "tenant": "migration"}))
            command = [sys.executable, "-B", "-m", "apps.oncall", "serve", "--out", str(state),
                       "--resume", "--patrol-only", "--no-learning"]

            def wait(process, predicate):
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("Service exited early: " + str(process.returncode))
                    try:
                        result = predicate()
                        if result:
                            return result
                    except (OSError, ValueError, KeyError, sqlite3.Error):
                        pass
                    time.sleep(.2)
                self.fail("Service did not make progress")

            def counts():
                result = {}
                for alias in ("instance-a", "instance-b"):
                    db_path = state / "patrols" / alias / "patrol.sqlite"
                    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
                        result[alias] = db.execute("SELECT count(*) FROM inspections").fetchone()[0]
                return result if all(result.values()) else None

            def session_ids():
                result = {}
                for alias in ("instance-a", "instance-b"):
                    database = state / "patrols" / alias / "hermes/state.db"
                    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                        result[alias] = {row[0] for row in db.execute("SELECT id FROM sessions")}
                return result

            def stop(process):
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=30)

            with (root / "first.log").open("w") as output:
                first = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
                try:
                    initial = wait(first, counts)
                    original = json.loads((state / "service-binding.json").read_text())
                    original_sessions = session_ids()
                finally:
                    stop(first)
            refused = subprocess.run([*command, "--memory-config", str(template)], cwd=ROOT,
                                     capture_output=True, text=True, timeout=15)
            self.assertNotEqual(refused.returncode, 0)
            self.assertNotIn("memory_configuration", json.loads((state / "service-binding.json").read_text())["configuration"])
            with (root / "second.log").open("w") as output:
                second = subprocess.Popen([*command, "--memory-config", str(template), "--enable-memory"],
                    cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
                try:
                    after = wait(second, lambda: counts() if all(v > initial[k] for k, v in (counts() or {}).items()) else None)
                    migrated = json.loads((state / "service-binding.json").read_text())
                    self.assertEqual(migrated["environment_ids"], original["environment_ids"])
                    self.assertEqual(migrated["migrations"][0]["kind"], "enable-openviking-memory")
                    self.assertTrue(all(after[k] > initial[k] for k in initial))
                    self.assertTrue(all(original_sessions[k] <= v for k, v in session_ids().items()))
                finally:
                    stop(second)


if __name__ == "__main__":
    unittest.main()

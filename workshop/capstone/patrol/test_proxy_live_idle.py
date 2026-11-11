"""Slow upstream generation must not archive a native incident mid-request."""

from contextlib import closing
from importlib.util import find_spec
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from environment import classify, get_snapshot, inject_fault, read_alert
from fixture import respond
from hermes_runtime import ONCALL_SKILL, PATROL_SKILL, SOURCE, install_oncall_skill, open_sessions
from patrol import connect, inspect_service
from skillclaw_bridge import incident_archive


@unittest.skipUnless((SOURCE / "run_agent.py").is_file() and find_spec("skillclaw") is not None
                     and find_spec("dotenv") is not None,
                     "Run native proxy checks with the pinned Hermes and SkillClaw environment")
class SlowProxyArchiveTests(unittest.TestCase):
    def test_report_taking_more_than_five_seconds_keeps_its_native_tool_receipts(self):
        capstone = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(capstone))
        from local_model import serve
        from oncall_service import GROUP, client_proxy

        def wait(action, predicate, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    value = action()
                    if predicate(value):
                        return value
                except (OSError, ValueError, RuntimeError, KeyError):
                    pass
                time.sleep(.1)
            self.fail("Native proxy/environment evidence did not become ready")

        delays = []

        def slow_model(messages):
            name, value = respond(messages)
            if name is None:
                before = time.monotonic()
                time.sleep(6)
                delays.append(time.monotonic() - before)
            return name, value

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, state = root / "environment", root / "patrol"
            environment.mkdir()
            state.mkdir()
            server = subprocess.Popen([sys.executable, "-B", str(capstone / "patrol/environment.py"),
                                       "--state", str(environment), "serve"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait(lambda: get_snapshot(environment, "payments"), lambda row: row["requests"] >= 10)
                inject_fault(environment, "payments", "on")
                wait(lambda: get_snapshot(environment, "payments"), lambda row: classify(row) == "abnormal")
                alert = wait(lambda: read_alert(environment, "payments"), bool)
                with serve(slow_model) as upstream, open_sessions(state) as sessions, closing(connect(state)) as db:
                    skill = install_oncall_skill(state)
                    patrol_skill = install_oncall_skill(state, name=PATROL_SKILL)
                    options = {"base_url": upstream, "api_key": "local-fixture", "model": "patrol-fixture"}
                    with client_proxy(root / "archives", "instance-a", state / "hermes/skills", options) as proxy:
                        record = inspect_service(sessions, db, "payments", environment,
                                                 {**options, "base_url": proxy}, skill,
                                                 patrol_skill_sha=patrol_skill, incoming_alert=alert,
                                                 capture_session=True, fresh_session=True, auto_recover=False)

                        def selected():
                            return incident_archive(root / "archives", "instance-a", record["session_id"],
                                                    group_id=GROUP, incident_id=record["incident_id"],
                                                    skill_sha256=record["skill_sha256"])

                        archive = json.loads(wait(selected, bool).read_text())
                        # Verify while the proxy is still running: shutdown must
                        # not be what repairs a prematurely closed session.
                        self.assertFalse(archive["turns"][-1]["tool_calls"])
                        self.assertEqual(archive["session_id"], record["session_id"])
                        names = {receipt["name"] for turn in archive["turns"] for receipt in turn["tool_results_raw"]}
                        self.assertTrue({"service_snapshot", "skill_view", "oncall_probe"} <= names)
                        self.assertGreaterEqual(min(delays), 5.5)
                        self.assertEqual(record["status"], "abnormal")
            finally:
                server.send_signal(signal.SIGINT)
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    unittest.main()

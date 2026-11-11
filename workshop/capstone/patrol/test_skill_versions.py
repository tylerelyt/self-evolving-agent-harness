"""Regression checks for tasks that retain a Skill across release and restart."""

from contextlib import closing
import hashlib
from importlib.util import find_spec
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from environment import alert_event
from hermes_runtime import (ONCALL_SKILL, PATROL_SKILL, SOURCE, install_oncall_skill,
                            open_sessions, read_oncall_skill)
from investigation import investigate
from patrol import connect, incidents, inspect_service, model_connection


@unittest.skipUnless((SOURCE / "run_agent.py").is_file() and find_spec("dotenv") is not None,
                     "Run native Skill tests with the pinned Hermes environment and site packages")
class SkillVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.active = self.state / "hermes" / "skills" / ONCALL_SKILL / "SKILL.md"
        self.versions = self.active.parent / "references" / "versions"

    def new_version(self):
        source = self.state / "new-version.md"
        source.write_bytes(self.active.read_bytes() + b"\nAdditional approved operating note.\n")
        return source

    def test_new_release_keeps_old_content_readable_through_native_skill_view(self):
        with open_sessions(self.state):
            old = install_oncall_skill(self.state)
            old_content = self.active.read_text()
            new = install_oncall_skill(self.state, self.new_version())
            active_content = self.active.read_bytes()
            self.assertNotEqual(old, new)
            self.assertEqual(read_oncall_skill(old)["content"], old_content)
            self.assertEqual(read_oncall_skill(new)["sha256"], new)
            self.assertEqual(self.active.read_bytes(), active_content)

    def test_legacy_active_file_is_archived_before_upgrade(self):
        with open_sessions(self.state):
            old = install_oncall_skill(self.state)
            shutil.rmtree(self.versions)
            self.assertEqual(read_oncall_skill(old)["sha256"], old)
            install_oncall_skill(self.state, self.new_version())
            self.assertEqual(read_oncall_skill(old)["sha256"], old)

    def test_corrupt_snapshot_cannot_fall_back_to_matching_active_file(self):
        with open_sessions(self.state):
            old = install_oncall_skill(self.state)
            active_content = self.active.read_bytes()
            (self.versions / f"{old}.md").write_bytes(b"corrupt saved version\n")
            with self.assertRaisesRegex(RuntimeError, "pinned version"):
                read_oncall_skill(old)
            with self.assertRaisesRegex(RuntimeError, "content hash"):
                install_oncall_skill(self.state, self.new_version())
            self.assertEqual(self.active.read_bytes(), active_content)

    def test_old_incident_recovers_missing_snapshot_from_its_original_skill_item(self):
        stamp = time.time()
        metrics = {"source": "live_lab_http_requests", "service": "payments", "environment_id": "lab-a",
                   "window_start": stamp - 30, "window_end": stamp, "requests": 100,
                   "error_rate": .1, "p95_ms": 10}
        probe = {"service": "payments", "environment_id": "lab-a", "path": "/work",
                 "http_status": 503, "checked_at": stamp + 1}
        with model_connection(True) as url, open_sessions(self.state) as sessions, closing(connect(self.state)) as db:
            options = {"base_url": url, "api_key": "local-fixture", "model": "patrol-fixture"}
            old = install_oncall_skill(self.state)
            patrol_skill = install_oncall_skill(self.state, name=PATROL_SKILL)
            inspect_service(sessions, db, "payments", self.state, options, old,
                            patrol_skill_sha=patrol_skill, snapshot=lambda *_: metrics,
                            probe_request=lambda *_args, **_kwargs: probe,
                            incoming_alert=alert_event(metrics, emitted_at=stamp - 1),
                            auto_recover=False)
            record = incidents(db, "payments")[0]
            new = install_oncall_skill(self.state, self.new_version())
            (self.versions / f"{old}.md").unlink()
            with patch("investigation.get_snapshot", return_value=metrics), patch("investigation.probe_service", return_value=probe):
                result = investigate(self.state, self.state, record, options, sessions=sessions)
            loaded = next(row for row in sessions.get_messages(result["session_id"])
                          if row["id"] == result["skill_item_id"])
            receipt = json.loads(loaded["content"])
            self.assertEqual(result["skill_sha256"], old)
            self.assertEqual(hashlib.sha256(receipt["content"].encode()).hexdigest(), old)
            self.assertEqual(read_oncall_skill(new)["sha256"], new)
            self.assertEqual(hashlib.sha256(self.active.read_bytes()).hexdigest(), new)
            self.assertTrue((self.versions / f"{old}.md").is_file())


if __name__ == "__main__":
    unittest.main()

"""Keep each proxy-captured incident in a fresh, explicitly identified Session."""

from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import patrol
from environment import publish_alert


class ProxyAlertCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.environment = self.state / "environment"
        self.environment.mkdir()
        self.alert = {"incident_id": "lab-payments-0123456789ab", "service": "payments",
                      "environment_id": "test-environment", "source": patrol.ALERT_SOURCE, "emitted_at": 20}
        self.metrics = {"source": "live_lab_http_requests", "service": "payments",
                        "environment_id": "test-environment", "window_start": 0, "window_end": 30,
                        "requests": 40, "error_rate": .25, "p95_ms": 10}
        self.skill_sha = "a" * 64

    def test_alert_arriving_during_ordinary_turn_waits_for_captured_turn(self):
        published = False
        chosen_turns, deliveries = [], []
        inspect = patrol.inspect_service

        def pending(*_args, **kwargs):
            self.assertEqual(kwargs["environment_id"], self.metrics["environment_id"])
            return self.alert if published else None

        def snapshot(*_args):
            nonlocal published
            # The monitor emits after run() has made this Turn's capture decision.
            published = True
            return dict(self.metrics)

        def inspect_with_late_snapshot(*args, **kwargs):
            chosen_turns.append({name: kwargs[name] for name in ("incoming_alert", "capture_session", "fresh_session")})
            return inspect(*args, snapshot=snapshot, **kwargs)

        def model_turn(_sessions, _session_id, _prompt, tools, _options, on_complete, **_kwargs):
            tools["patrol_skill_view"]()
            receipt = json.dumps(tools["service_snapshot"]())
            on_complete(lambda _instruction: True, "service_snapshot", receipt)
            # A rejected steer lets this test check selection without fabricating
            # an incident report, a native Session or a SkillClaw archive.
            rows = [{"id": 1, "role": "assistant", "tool_calls": [
                {"id": "snapshot", "function": {"name": "service_snapshot"}}]},
                {"id": 2, "role": "tool", "tool_call_id": "snapshot", "content": receipt},
                {"id": 3, "role": "assistant", "content": json.dumps({
                    "status": "abnormal", "summary": "The current HTTP window has failures.", "history_id": None})}]
            return rows, []

        def reject_steer(_steer, alert):
            deliveries.append(alert)
            return False

        with (closing(patrol.connect(self.state)) as db,
              patch.object(patrol, "install_oncall_skill", return_value=self.skill_sha),
              patch.object(patrol, "read_oncall_skill", return_value={"sha256": self.skill_sha}),
              patch.object(patrol, "get_snapshot", return_value=self.metrics),
              patch.object(patrol, "read_alert", side_effect=pending) as read_alert,
              patch.object(patrol, "inspect_service", side_effect=inspect_with_late_snapshot),
              patch.object(patrol, "run_turn", side_effect=model_turn),
              patch.object(patrol, "steer_oncall", side_effect=reject_steer),
              patch.object(patrol, "show")):
            patrol.run(self.state, db, object(), {}, 0, 1, 2, True,
                       environment_state=self.environment, capture_session=True, services=["payments"])

        self.assertEqual(chosen_turns, [
            {"incoming_alert": {}, "capture_session": False, "fresh_session": False},
            {"incoming_alert": self.alert, "capture_session": True, "fresh_session": True},
        ])
        self.assertEqual(read_alert.call_count, 2)
        self.assertEqual(deliveries, [self.alert])

    def test_explicit_incident_is_captured_even_if_no_pending_event_was_selected(self):
        path = self.environment / "alerts" / "payments" / f"{self.alert['incident_id']}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.alert))
        with (closing(patrol.connect(self.state)) as db,
              patch.object(patrol, "install_oncall_skill", return_value=self.skill_sha),
              patch.object(patrol, "get_snapshot", return_value=self.metrics),
              patch.object(patrol, "read_alert", return_value=None),
              patch.object(patrol, "inspect_service", return_value={"id": 1}) as inspect,
              patch.object(patrol, "show")):
            patrol.run(self.state, db, object(), {}, 0, 1, 1, True,
                       environment_state=self.environment, capture_session=True, services=["payments"],
                       incident_id=self.alert["incident_id"])
        selected = inspect.call_args.kwargs
        self.assertEqual(selected["incoming_alert"], self.alert)
        self.assertTrue(selected["capture_session"])
        self.assertTrue(selected["fresh_session"])

    def test_events_from_an_older_environment_do_not_block_current_capture(self):
        old = {**self.alert, "incident_id": "lab-payments-000000000001",
               "environment_id": "previous-environment", "emitted_at": 10}
        publish_alert(self.environment, old)
        publish_alert(self.environment, self.alert)
        with (closing(patrol.connect(self.state)) as db,
              patch.object(patrol, "install_oncall_skill", return_value=self.skill_sha),
              patch.object(patrol, "get_snapshot", return_value=self.metrics),
              patch.object(patrol, "inspect_service", return_value={"id": 1}) as inspect,
              patch.object(patrol, "show")):
            patrol.run(self.state, db, object(), {}, 0, 1, 1, True,
                       environment_state=self.environment, capture_session=True, services=["payments"])
        selected = inspect.call_args.kwargs
        self.assertEqual(selected["incoming_alert"], self.alert)
        self.assertTrue(selected["capture_session"])
        self.assertTrue(selected["fresh_session"])
        self.assertTrue((self.environment / "alerts" / "payments" / f"{old['incident_id']}.json").is_file())

    def test_patrol_without_proxy_keeps_turn_time_alert_delivery(self):
        with (closing(patrol.connect(self.state)) as db,
              patch.object(patrol, "install_oncall_skill", return_value=self.skill_sha),
              patch.object(patrol, "read_alert") as read_alert,
              patch.object(patrol, "inspect_service", return_value={"id": 1}) as inspect,
              patch.object(patrol, "show")):
            patrol.run(self.state, db, object(), {}, 0, 1, 1, True,
                       environment_state=self.environment, services=["payments"])
        selected = inspect.call_args.kwargs
        self.assertIsNone(selected["incoming_alert"])
        self.assertFalse(selected["capture_session"])
        self.assertFalse(selected["fresh_session"])
        read_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()

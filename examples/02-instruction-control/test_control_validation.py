"""离线反例：验证验收逻辑不会把报告字样、旧标记或未结束线程算作成功。"""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from control_validation import TurnTrace, patrol_checks, read_targets


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.work = Path('/tmp/lab02-test')
        self.files = {f'servers/{s}/{f}.md': f'# {s} {f}\n材料编号：OBS-{s}-{f}\n'
                      for s in ('server-17', 'server-31') for f in ('deployment', 'metrics')}
        self.events = []
        for i, (path, content) in enumerate(self.files.items()):
            item = {'id': str(i), 'type': 'commandExecution', 'command': f'cat {path}',
                    'cwd': str(self.work), 'status': 'completed', 'exit_code': 0, 'aggregated_output': content}
            for method in ('item/started', 'item/completed'):
                self.events.append({'seq': len(self.events), 'method': method, 'item': copy.deepcopy(item)})
        self.report = '已生成教学报告；结论和引用由读者核对。'

    def checks(self, events=None, **overrides):
        args = dict(events=self.events if events is None else events, work=self.work, files=self.files,
                    before_steer=4, after_steer=4, status='completed', same_turn=True,
                    incident_text=self.report, patrol_text=self.report, unchanged=True)
        args.update(overrides)
        return patrol_checks(**args)[0]

    def test_complete_receipts_pass(self):
        self.assertTrue(all(self.checks().values()))

    def test_report_alone_fails(self):
        self.assertFalse(all(self.checks(events=[]).values()))
        self.assertFalse(all(self.checks(incident_text='', patrol_text='').values()))

    def test_failed_wrong_output_or_wrong_object_fails(self):
        for field, value in [('exit_code', 1), ('aggregated_output', 'success'), ('command', 'cat servers/server-99/metrics.md'),
                             ('cwd', '/tmp/other'), ('status', 'inProgress')]:
            with self.subTest(field=field):
                events = copy.deepcopy(self.events)
                events[-1]['item'][field] = value
                self.assertFalse(all(self.checks(events).values()))

    def test_reads_before_alert_do_not_prove_resumption(self):
        self.assertFalse(all(self.checks(before_steer=8, after_steer=8).values()))

    def test_read_started_before_alert_does_not_prove_resumption(self):
        self.assertFalse(all(self.checks(after_steer=5).values()))

    def test_old_material_reread_fails_reuse(self):
        events = copy.deepcopy(self.events)
        for event in copy.deepcopy(events[:2]):
            event['seq'] = len(events)
            event['item']['id'] = 'reread'
            events.append(event)
        self.assertFalse(self.checks(events)['告警后没有再次取得 server-17 原材料'])

    def test_missing_start_failed_turn_changed_input_or_wrong_turn(self):
        events = [e for e in self.events if e['method'] != 'item/started']
        self.assertFalse(all(self.checks(events).values()))
        for override in [{'same_turn': False}, {'status': 'failed'}, {'unchanged': False}]:
            self.assertFalse(all(self.checks(**override).values()))

    def test_echo_and_compound_commands_are_not_reads(self):
        for command in ['echo cat servers/server-17/deployment.md',
                        'cat servers/server-17/deployment.md; echo success',
                        'cat servers/server-17/deployment.md > report.md',
                        "python -c 'print(123)'", 'cat ../other/file.md']:
            self.assertEqual(read_targets({'command': command, 'cwd': str(self.work)}, self.work), set())
        self.assertEqual(read_targets({'command': '/bin/zsh -lc "cat servers/server-17/deployment.md"',
                                       'cwd': str(self.work)}, self.work), {'servers/server-17/deployment.md'})


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.turn = Mock()
        self.trace = TurnTrace(self.turn)
        self.trace.worker = Mock()
        self.trace.worker.is_alive.return_value = True

    def test_marker_timeout_does_not_steer(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TimeoutError):
                self.trace.wait_for_marker(Path(directory) / 'missing', 0.001)
        self.turn.steer.assert_not_called()

    def test_existing_marker_without_receipts_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'marker'; marker.touch()
            with self.assertRaises(TimeoutError):
                self.trace.wait_for_marker(marker, 0.001, ready=lambda: False)
        self.turn.steer.assert_not_called()

    def test_ended_worker_or_exception_fails_before_steer(self):
        self.trace.worker.is_alive.return_value = False
        with self.assertRaises(RuntimeError): self.trace.wait_for_marker(Path('/tmp/missing'), 1)
        self.trace.error = ValueError('后台异常')
        with self.assertRaises(RuntimeError): self.trace.wait_for_marker(Path('/tmp/missing'), 1)
        self.turn.steer.assert_not_called()

    def test_join_rejects_running_worker(self):
        with self.assertRaises(TimeoutError): self.trace.join(0)

    def test_join_surfaces_worker_exception_and_missing_terminal(self):
        self.trace.worker.is_alive.return_value = False
        with self.assertRaises(RuntimeError): self.trace.join(0)
        self.trace.error = ValueError('后台异常')
        with self.assertRaises(RuntimeError): self.trace.join(0)

    def test_abort_requires_worker_exit(self):
        with self.assertRaises(RuntimeError): self.trace.abort()
        self.turn.interrupt.assert_called_once()

    def test_stream_exception_is_saved(self):
        self.turn.stream.side_effect = RuntimeError('事件流失败')
        self.trace._consume()
        self.assertIsInstance(self.trace.error, RuntimeError)


class SdkEventTests(unittest.TestCase):
    def test_fixed_sdk_notification_shapes(self):
        # 使用实际 SDK 类型构造事件，检查模型字段、状态枚举与序列化接口。
        from openai_codex.models import Notification
        from openai_codex.generated.v2_all import (
            ThreadItem, ItemStartedNotification, ItemCompletedNotification,
            Turn, TurnCompletedNotification,
        )
        turn = Mock(id="test-turn")
        item = ThreadItem.model_validate({
            "type": "commandExecution", "id": "read-1", "command": "cat servers/server-17/deployment.md",
            "commandActions": [], "cwd": "/tmp/lab02-test", "status": "completed", "source": "agent",
            "exitCode": 0, "aggregatedOutput": "材料内容",
        })
        turn.stream.return_value = iter([
            Notification(method="item/started", payload=ItemStartedNotification(
                item=item, started_at_ms=1, thread_id="test-thread", turn_id=turn.id)),
            Notification(method="item/completed", payload=ItemCompletedNotification(
                item=item, completed_at_ms=2, thread_id="test-thread", turn_id=turn.id)),
            Notification(method="turn/completed", payload=TurnCompletedNotification(
                thread_id="test-thread", turn=Turn(id=turn.id, items=[item], status="completed"))),
        ])
        trace = TurnTrace(turn).start()
        self.assertEqual(trace.join(2), "completed")
        events = trace.snapshot()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["item"]["exit_code"], 0)
        self.assertEqual(events[-1]["item"]["status"], "completed")
        self.assertEqual(events[-1]["item"]["cwd"], "/tmp/lab02-test")

    def test_environment_installs_wrong_version_then_requires_restart(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import notebook02_setup as setup
        with patch("sys.version_info", (3, 12)), patch.object(setup, "version", return_value="0.153.0"), \
                patch("subprocess.run") as install, patch.dict("sys.modules", {"openai_codex": Mock()}):
            with self.assertRaisesRegex(RuntimeError, "依赖已安装；请重启内核"):
                setup.prepare()
            install.assert_called_once()
            arguments = install.call_args.args[0]
            self.assertIn("openai-codex==0.154.0", arguments)
            self.assertIn("litellm[proxy]==1.101.0", arguments)
        with patch("sys.version_info", (3, 11)):
            with self.assertRaisesRegex(RuntimeError, "Python 3.12"):
                setup.prepare()


if __name__ == '__main__':
    unittest.main(verbosity=2)

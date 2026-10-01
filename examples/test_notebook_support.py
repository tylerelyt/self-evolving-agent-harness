"""核对等待保护与结果记录；测试不启动模型或 app-server。"""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from notebook_support import Lab


class NotebookExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = object.__new__(Lab)
        self.lab.runtime = Path(self.temp.name)
        self.lab._secrets = []
        self.lab.turn_objects, self.lab.results = [], []

    def test_sdk_run_result_is_not_replaced(self):
        turn = SimpleNamespace(id='turn-one', thread_id='thread-one')
        result = SimpleNamespace(id='turn-one', status=SimpleNamespace(value='completed'),
                                 final_response='SDK实际返回', items=[])
        with self.lab.watch(turn, timeout=1, progress_every=1):
            actual = result
        self.assertIs(self.lab.record_result(turn, actual, show_result=False), result)
        self.assertEqual(self.lab.results[0]['thread_id'], 'thread-one')

    def test_timeout_interrupts_sdk_handle_and_reports_failure(self):
        interrupted = threading.Event()
        turn = SimpleNamespace(id='slow-turn', thread_id='thread-one', interrupt=interrupted.set)
        with self.assertRaises(TimeoutError):
            with self.lab.watch(turn, timeout=0.03, progress_every=1):
                self.assertTrue(interrupted.wait(2))
        self.assertTrue((self.lab.runtime / 'turn-failure.json').exists())
        self.assertEqual(self.lab.results, [])

    def test_failed_sdk_result_is_not_reported_as_completed(self):
        turn = SimpleNamespace(id='failed-turn', thread_id='thread-one')
        result = SimpleNamespace(id='failed-turn', status=SimpleNamespace(value='failed'),
                                 final_response='', items=[])
        with self.assertRaises(AssertionError):
            self.lab.record_result(turn, result, show_result=False)
        self.assertEqual(self.lab.results[0]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()

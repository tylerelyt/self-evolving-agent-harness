"""构造的已有测试：确认默认值；Hotfix 后应补齐显式传参测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lib.database.pool import acquire


class SharedDefaultTests(unittest.TestCase):
    def test_shared_default_stays_thirty(self):
        self.assertEqual(acquire()["wait_seconds"], 30)


if __name__ == "__main__":
    unittest.main()

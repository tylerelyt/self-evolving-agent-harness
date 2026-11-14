"""The isolated revision entry must not invoke a model without explicit --live."""

import os
from pathlib import Path
import subprocess
import unittest

HERE = Path(__file__).resolve().parent


class RevisionEntryTests(unittest.TestCase):
    def test_missing_or_unrecognized_live_flag_stops_before_runtime_creation(self):
        before = set((HERE / 'runtime').glob('native-*'))
        environment = {k: v for k, v in os.environ.items() if k not in ('GLM_API_KEY', 'BIGMODEL_API_KEY')}
        for arguments in ([], ['--check-local'], ['--live', '--extra']):
            with self.subTest(arguments=arguments):
                result = subprocess.run(['bash', str(HERE / 'run.sh'), 'revision', *arguments],
                                        env=environment, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn('revision --live', result.stderr)
        self.assertEqual(set((HERE / 'runtime').glob('native-*')), before)


if __name__ == '__main__':
    unittest.main()

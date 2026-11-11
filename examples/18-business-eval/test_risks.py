"""离线检查新主练习的比较、连接、隔离和评分，不实例化模型客户端。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from common import HERE, OFFICIAL_API, connection_config
from isolation import prepare
from alert_dataset import CASES
from alert_workshop import AlertEvaluation, compare
from alert_selftest import run


class RiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (HERE / 'runtime').mkdir(exist_ok=True)

    def test_scorer_controls(self):
        self.assertTrue(run()['passed'])

    def test_missing_sandbox_stops_before_dependencies_or_credentials(self):
        with patch('isolation.SANDBOX', Path('/not-installed/sandbox-exec')), \
             patch('alert_workshop.verify_dependency') as dependency, \
             patch('alert_workshop.credentials') as credentials:
            with self.assertRaisesRegex(RuntimeError, 'sandbox-exec'):
                AlertEvaluation()
        dependency.assert_not_called()
        credentials.assert_not_called()

    def test_missing_optional_review_folders_are_not_required_probes(self):
        protocol = (HERE / 'alert_protocol.json').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'alert_protocol.json').write_text(protocol)
            with patch('alert_workshop.HERE', root), \
                 patch('alert_workshop.require_backend'), \
                 patch('alert_workshop.verify_dependency', return_value='pinned'), \
                 patch('alert_workshop.source_hashes', return_value={}):
                evaluation = AlertEvaluation()
            self.assertNotIn(root / 'validation', evaluation.forbidden)
            self.assertNotIn(root / 'fixtures', evaluation.forbidden)
            self.assertIn(root / 'alert_dataset.py', evaluation.forbidden)
            self.assertIn(evaluation.root / 'expected.json', evaluation.forbidden)

    def test_comparison_rejects_regression_or_mismatched_inputs(self):
        rows = [{'version': v, 'case_id': c['id'], 'repeat': 1,
                 'input_sha256': 'input', 'snapshot_sha256': 'snapshot', 'scorer_sha256': 'scorer',
                 'expected_sha256': 'expected', 'protocol_sha256': 'protocol', 'model_config': {'same': True},
                 'grading': {'score': {'overall_pass': not (v == 'after' and c['id'] == 'A01')}}}
                for c in CASES for v in ('before', 'after')]
        self.assertIn('拒绝采用', compare(rows, 'after', 1)['recommendation'])
        with self.assertRaisesRegex(ValueError, '重复'):
            compare(rows + [dict(rows[0])], 'after', 1)
        for field in ('input_sha256', 'snapshot_sha256', 'scorer_sha256', 'expected_sha256', 'protocol_sha256', 'model_config'):
            altered = [dict(r) for r in rows]; altered[1][field] = 'changed'
            self.assertIn('未完成', compare(altered, 'after', 1)['recommendation'])
        rows[1]['grading']['score'] = None
        self.assertIn('未完成', compare(rows, 'after', 1)['recommendation'])

    def test_connection_override_does_not_change_protocol(self):
        protocol = json.loads((HERE / 'alert_protocol.json').read_text())
        before = dict(protocol)
        with patch.dict(os.environ, {'GLM_BASE_URL': 'http://127.0.0.1:8765/v1'}):
            config = connection_config(protocol)
        self.assertEqual(config['execution_endpoint'], 'http://127.0.0.1:8765/v1')
        self.assertEqual(config['connection_mode'], 'loopback-proxy')
        self.assertIsNone(config['upstream_endpoint'])
        self.assertFalse(config['upstream_verified'])
        self.assertEqual(protocol, before)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(connection_config(protocol)['execution_endpoint'], OFFICIAL_API)
        for bad in ('http://remote.example/v4', 'https://user:token@example.com/v4', 'https://example.com/v4?key=x', ''):
            with patch.dict(os.environ, {'GLM_BASE_URL': bad}), self.assertRaises(ValueError):
                connection_config(protocol)

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').is_file(), '本机不是 macOS 沙箱后端')
    def test_real_sandbox_denies_reference_file(self):
        with tempfile.TemporaryDirectory(dir=HERE / 'runtime') as directory:
            root = Path(directory)
            work = root / 'work'; work.mkdir()
            source = root / 'dependency'; source.mkdir()
            answer = root / 'expected.json'; answer.write_text('{"constructed":true}')
            visible = work / 'input.json'; visible.write_text('{"task":"constructed"}')
            _, profile = prepare(work, Path('/usr/bin/python3'), source, [answer])
            allowed = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), '/bin/cat', str(visible)], capture_output=True)
            denied = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), '/bin/cat', str(answer)], capture_output=True)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertNotEqual(denied.returncode, 0)
            self.assertNotIn(b'constructed', denied.stdout)

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').is_file(), '本机不是 macOS 沙箱后端')
    def test_broad_source_root_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=HERE / 'runtime') as directory:
            with self.assertRaisesRegex(RuntimeError, '暴露答案'):
                prepare(Path(directory), Path('/usr/bin/python3'), HERE.parent, [])


if __name__ == '__main__':
    unittest.main()

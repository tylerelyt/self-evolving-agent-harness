"""采用范围与归档前读取的离线反例，不调用 Hermes 或模型。"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import HERE
from adoption import adopt_package, package_manifest
from curator_read_adapter import CuratorReadAdapter
from curation_workshop import review_outcome, extract_answer


class RiskTests(unittest.TestCase):
    def test_json_envelope_does_not_hide_ambiguity(self):
        import json
        answer = {'diagnosis': 'capacity-pressure', 'next_check': '检查慢查询'}
        raw = json.dumps(answer)
        block = '```json\n' + raw + '\n```'
        self.assertTrue(extract_answer(raw)['strict_json_format_pass'])
        self.assertEqual(extract_answer(block)['answer'], answer)
        self.assertFalse(extract_answer(block)['strict_json_format_pass'])
        self.assertIsNone(extract_answer(block + '\n' + block)['answer'])
        self.assertIsNone(extract_answer('{"diagnosis":"unknown","diagnosis":"capacity-pressure"}')['answer'])
        self.assertEqual(extract_answer('实际不是容量问题\n' + block)['outside_text'], '实际不是容量问题')

    def test_no_op_requires_completed_current_run(self):
        native = {'started_at': 'current', 'llm_error': None}
        returned = {'started_at': 'current'}
        loops = [{'completed': True}]
        self.assertEqual(review_outcome(native, returned, loops, {}, {}), 'no-op')
        self.assertEqual(review_outcome(native, returned, loops, {}, {'a': 'new'}), 'changed')
        self.assertEqual(review_outcome({**native, 'llm_error': 'failed'}, returned, loops, {}, {}), 'incomplete')
        self.assertEqual(review_outcome(native, {'started_at': 'old'}, loops, {}, {}), 'incomplete')
        self.assertEqual(review_outcome(native, returned, [{'completed': False}], {}, {}), 'incomplete')

    def setUp(self):
        (HERE / 'runtime').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=HERE / 'runtime')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidate = self.root / 'candidate'; self.candidate.mkdir()
        self.formal = self.root / 'formal'; self.formal.mkdir()
        (self.formal / 'old.md').write_text('old')
        skill = self.candidate / 'general'; skill.mkdir()
        (skill / 'SKILL.md').write_text('references/detail.md')
        (skill / 'references').mkdir()
        (skill / 'references/detail.md').write_text('details')
        (self.candidate / '.usage.json').write_text('{}')

    def adopt(self, expected):
        return adopt_package(self.candidate, self.formal, self.root / 'stage', self.root / 'previous', expected)

    def test_hidden_state_is_checked(self):
        expected = package_manifest(self.candidate)
        (self.candidate / '.usage.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.adopt(expected)
        self.assertEqual((self.formal / 'old.md').read_text(), 'old')

    def test_binary_resource_is_checked(self):
        (self.candidate / 'general/resource.bin').write_bytes(bytes([0, 255, 1]))
        expected = package_manifest(self.candidate)
        self.adopt(expected)
        self.assertEqual(package_manifest(self.formal), expected)
        self.assertTrue((self.root / 'previous/old.md').exists())

    def test_copy_race_keeps_formal_directory(self):
        import shutil
        original = shutil.copytree
        def changed_copy(source, destination, *args, **kwargs):
            result = original(source, destination, *args, **kwargs)
            if Path(destination) == self.root / 'stage':
                (Path(destination) / '.usage.json').write_text('{"changed":true}')
            return result
        with patch('adoption.shutil.copytree', side_effect=changed_copy), self.assertRaises(ValueError):
            self.adopt(package_manifest(self.candidate))
        self.assertTrue((self.formal / 'old.md').exists())

    def test_symlink_is_rejected(self):
        (self.candidate / 'linked').symlink_to(self.formal)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            package_manifest(self.candidate)

    def test_changed_or_unread_support_file_blocks_archive(self):
        adapter = CuratorReadAdapter(self.candidate)
        for path in (self.candidate / 'general').rglob('*'):
            if path.is_file():
                adapter.reads[path.resolve()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(adapter.unread_target_files('general'), [])
        (self.candidate / 'general/references/detail.md').write_text('changed')
        self.assertEqual(adapter.unread_target_files('general'), ['general/references/detail.md'])
        with self.assertRaises(ValueError):
            adapter.unread_target_files('../formal')


if __name__ == '__main__':
    unittest.main()

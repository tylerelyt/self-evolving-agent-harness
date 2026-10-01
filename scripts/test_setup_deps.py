"""用临时 Git 仓库确认环境检查失败时不会改写已有工作；不联网或安装。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('setup_deps.sh')


class DependencyCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        shutil.copy2(SCRIPT, self.root / 'scripts/setup_deps.sh')
        self.pin = self.root / 'scripts/hermes-revision.txt'
        self.pin.write_text('a' * 40 + '\n')

    def check_environment(self):
        return subprocess.run(['bash', str(self.root / 'scripts/setup_deps.sh'), '--check'],
                              text=True, capture_output=True, cwd=self.root)

    def repository(self):
        source = self.root / '.deps/hermes-agent'
        source.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        (source / 'source.txt').write_text('原文件\n')
        subprocess.run(['git', '-C', str(source), 'add', 'source.txt'], check=True)
        env = {**os.environ, 'GIT_AUTHOR_DATE': '2026-10-01T10:00:00+08:00',
               'GIT_COMMITTER_DATE': '2026-10-01T10:00:00+08:00'}
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '-qm', '测试提交'], check=True, env=env)
        head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        return source, head

    def test_check_does_not_prepare_missing_source(self):
        result = self.check_environment()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('尚未准备', result.stderr)
        self.assertFalse((self.root / '.deps').exists())

    def test_wrong_revision_keeps_existing_checkout(self):
        source, head = self.repository()
        result = self.check_environment()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不是课程固定提交', result.stderr)
        self.assertEqual(subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(), head)
        self.assertEqual((source / 'source.txt').read_text(), '原文件\n')
        self.assertFalse((source / '.venv').exists())

    def test_dirty_source_is_rejected_without_overwrite(self):
        source, head = self.repository()
        self.pin.write_text(head + '\n')
        (source / 'source.txt').write_text('用户未提交的修改\n')
        result = self.check_environment()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已跟踪文件修改', result.stderr)
        self.assertEqual((source / 'source.txt').read_text(), '用户未提交的修改\n')


if __name__ == '__main__':
    unittest.main()

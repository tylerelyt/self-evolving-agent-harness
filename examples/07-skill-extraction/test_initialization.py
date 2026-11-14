"""Standard-library contracts for actual initializer selections; no model calls."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from initialization import resources_match

HERE = Path(__file__).resolve().parent


class InitializationTests(unittest.TestCase):
    def test_actual_initializer_matches_empty_single_and_three_resource_selections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {'PATH': os.defpath, 'HOME': directory, 'PYTHONDONTWRITEBYTECODE': '1'}
            for index, selected in enumerate(([], ['references'], ['scripts', 'references', 'assets'])):
                name = 'selection-' + str(index)
                command = [sys.executable, '-E', '-S', '-B', str(HERE / 'skill_creator/scripts/init_skill.py'),
                           name, '--path', directory]
                if selected:
                    command += ['--resources', ','.join(selected)]
                done = subprocess.run(command, text=True, capture_output=True, env=env)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                skill = root / name
                created = [p.name for p in skill.iterdir() if p.is_dir()]
                self.assertTrue(resources_match(selected, created))
                self.assertTrue((skill / 'SKILL.md').is_file())
                self.assertTrue((skill / 'agents/openai.yaml').is_file())
                self.assertTrue(all(not list((skill / resource).iterdir()) for resource in selected))
                self.assertFalse(resources_match(selected + ['scripts'] if 'scripts' not in selected else [], created))

    def test_missing_extra_unknown_duplicate_or_malformed_selection_is_rejected(self):
        for selected, created in ((['scripts'], ['agents']), ([], ['agents', 'scripts']),
                (['unknown'], ['agents', 'unknown']), (['scripts', 'scripts'], ['agents', 'scripts']),
                (['references'], ['references']), ([], ['agents', 'agents']),
                ([1], ['agents']), ('scripts', ['agents', 'scripts'])):
            self.assertFalse(resources_match(selected, created), (selected, created))


if __name__ == '__main__':
    unittest.main(verbosity=2)

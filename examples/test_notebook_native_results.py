"""Offline failure-observation contracts; fixtures never call Hermes or a model."""
import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from notebook_native_results import start_run, collect_run, save_observation, show_observation

ROOT = Path(__file__).resolve().parent


class ObservationTests(unittest.TestCase):
    def test_nonzero_exit_preserves_current_partial_review_and_displays_it(self):
        for lesson in ('15-skill-autocreation', '16-skill-incremental-patch'):
            number = lesson[:2]
            with self.subTest(lesson=lesson), tempfile.TemporaryDirectory() as directory:
                here = Path(directory)
                (here / 'validation').mkdir()
                (here / 'validation/native-live-latest.json').write_text(json.dumps({'status': 'passed', 'report': 'old-success'}))
                fixture = '''import argparse,json,os,sys
from pathlib import Path
from notebook_native_results import start_run
p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');p.add_argument('--run-dir');a=p.parse_args()
r=start_run(Path(__file__).parent,os.environ.get('LESSON_NUMBER_VALIDATION_ID'),a.run_dir)
(r/'review.json').write_text(json.dumps({'result':{'completed':False,'final_response':'current-write-rejected'},'calls':[{'receipt':{'success':False,'error':'read-before-write'}}],'before':{},'after':{}}))
(r/'failure.json').write_text(json.dumps({'status':'failed','error':'current-write-rejected'}))
print('controlled child failure, not a model observation')
sys.exit(1)
'''.replace('LESSON_NUMBER', 'LESSON' + number)
                (here / 'native_workshop.py').write_text(fixture)
                notebook = json.loads((ROOT / lesson / 'workshop.ipynb').read_text())
                setup = ast.parse(''.join(notebook['cells'][1]['source']))
                stop = next(n for n in setup.body if isinstance(n, ast.FunctionDef) and n.name == 'stop_owned')
                scope = {'Path': Path, 'HERE': here, 'HERMES_PYTHON': Path(sys.executable), 'process': None,
                         'os': os, 'signal': signal, 'subprocess': subprocess,
                         'env': {**os.environ, 'PYTHONPATH': str(ROOT),
                                 'LESSON' + number + '_VALIDATION_ID': 'current-attempt'}}
                exec(compile(ast.Module(body=[stop], type_ignores=[]), '<stop-owned>', 'exec'), scope)
                output = io.StringIO()
                with patch.object(sys, 'path', [str(ROOT / lesson), *sys.path]), patch.dict(os.environ, {'LESSON' + number + '_RUN_MODEL': '1',
                        'LESSON' + number + '_VALIDATION_ID': 'current-attempt'}), redirect_stdout(output):
                    exec(''.join(notebook['cells'][7]['source']), scope)
                    exec(''.join(notebook['cells'][9]['source']), scope)
                result = scope['live_result']
                self.assertEqual(result['returncode'], 1)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('current-write-rejected', output.getvalue())
                self.assertNotIn('old-success', output.getvalue())
                self.assertIn('本次尚未取得', output.getvalue())
                saved = json.loads((here / 'validation/notebook-current-attempt.json').read_text())
                self.assertEqual(saved['attempt_id'], result['attempt_id'])
                self.assertFalse(saved['native_record_files_present'])

    def test_actual_native_preflight_failure_saves_this_attempt_without_model(self):
        for lesson in ('15-skill-autocreation', '16-skill-incremental-patch'):
            number = lesson[:2]
            here = ROOT / lesson
            attempt = 'native-preflight-check-' + uuid4().hex
            run = here / 'runtime' / attempt
            source = ROOT.parent / '.deps/hermes-agent'
            env = {**os.environ, 'GLM_API_KEY': '', 'HERMES_SRC': str(source),
                   'PYTHONPATH': str(source), 'LESSON' + number + '_VALIDATION_ID': attempt}
            try:
                child = subprocess.run([sys.executable, '-E', '-S', '-B', str(here / 'native_workshop.py'),
                    '--live', '--run-dir', str(run)], env=env, text=True, capture_output=True, timeout=15)
                self.assertNotEqual(child.returncode, 0)
                result = collect_run(run, attempt, child.returncode)
                self.assertTrue(result['attempt_started'])
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['native_record_files_present'])
                self.assertIn('GLM_API_KEY', result['artifacts']['failure.json']['error'])
                self.assertFalse(result['artifacts']['cleanup.json']['session_db_closed'])
            finally:
                shutil.rmtree(run, ignore_errors=True)

    def test_incomplete_zero_exit_is_not_success_and_old_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lesson = Path(directory)
            run = start_run(lesson, 'current')
            (run / 'result.json').write_text(json.dumps({'status': 'passed'}))
            self.assertEqual(collect_run(run, 'current', 0)['status'], 'failed')
            with self.assertRaisesRegex(ValueError, 'identity'):
                collect_run(run, 'different', 0)
            with self.assertRaises(FileExistsError):
                start_run(lesson, 'current', run)

    def test_no_update_is_kept_distinct_from_skill_learning_success(self):
        with tempfile.TemporaryDirectory() as directory:
            run = start_run(Path(directory), 'current')
            for name in ('investigation.json', 'review.json'):
                (run / name).write_text(json.dumps({'result': {'completed': True}, 'calls': []}))
            (run / 'result.json').write_text(json.dumps({'status': 'no-update', 'passed': False, 'skill_created': False}))
            result = collect_run(run, 'current', 0)
            self.assertEqual(result['status'], 'no-update')
            self.assertFalse(result['artifacts']['result.json']['skill_created'])
            self.assertEqual(collect_run(run, 'current', 1)['status'], 'failed')

    def test_malformed_review_and_timeout_remain_visible_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            run = start_run(Path(directory), 'current')
            (run / 'review.json').write_text('{incomplete')
            result = collect_run(run, 'current', -15, 'TimeoutExpired')
            self.assertEqual(result['status'], 'failed')
            self.assertIn('review.json', result['read_errors'])
            with redirect_stdout(io.StringIO()) as output:
                show_observation(result)
            self.assertIn('TimeoutExpired', output.getvalue())
            self.assertIn('review.json', output.getvalue())

    def test_missing_attempt_and_symlink_cannot_recover_a_historical_success(self):
        with tempfile.TemporaryDirectory() as directory:
            lesson = Path(directory)
            missing = collect_run(lesson / 'runtime/missing', None, None)
            self.assertEqual(missing['status'], 'not-started')
            self.assertFalse(missing['attempt_started'])
            old = start_run(lesson, 'old')
            alias = lesson / 'runtime/alias'
            alias.symlink_to(old, target_is_directory=True)
            with self.assertRaises(ValueError):
                collect_run(alias, 'old', 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)

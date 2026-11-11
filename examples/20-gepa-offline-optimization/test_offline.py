"""无模型调用的程序测试；构造轨迹只用于校准，不是 Hermes 成绩。"""
from copy import deepcopy
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import gepa
from support import HERE, LESSON18, body_hash, read_skill, write_skill
from adapter import OncallAdapter, business_grader
from alert_selftest import correct, run as scorer_selftest
from alert_scorer import grade, pairs
from dataset import SPLITS, public_input, validate_splits
from incidentctl import query
from optimize import search, select_candidate
from reflection import ReflectionLM, connection
from runner import HermesRunner


def constructed_result(task, body):
    execution = correct(task)
    # 第18讲校准器默认 prod-eu；这里改成该固定题的目标集群。
    for index, action in enumerate(('alerts', 'logs', 'traces')):
        message = execution['messages'][2 + index * 2]
        message['tool_calls'][0]['function']['arguments'] = json.dumps(
            {'command': f'./incidentctl {action} {task["expected"]["cluster"]} all'})
        execution['messages'][3 + index * 2]['content'] = json.dumps(
            {'exit_code': 0, 'output': json.dumps(query(task['snapshot'], action, task['expected']['cluster'], 'all'))})
    return {'execution': execution, 'report': execution['final_response'],
            'tool_calls': pairs(execution['messages']), 'skill_body_sha256': body_hash(body)}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        (HERE / 'runtime').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=HERE / 'runtime')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixed_dependency_and_splits(self):
        self.assertEqual(version('gepa'), '0.0.27')
        validate_splits()
        bad = deepcopy(SPLITS)
        bad['holdout'][0] = bad['train'][0]
        with self.assertRaises(ValueError):
            validate_splits(bad)
        for tasks in SPLITS.values():
            for task in tasks:
                self.assertEqual(set(public_input(task)), {'task', 'system'})
                self.assertNotIn('expected', task['snapshot'])
                self.assertNotIn('required_citations', task['snapshot'])
                self.assertTrue(all('role' not in row for row in task['snapshot']['logs']))

    def test_inspect_dataset_runs_without_site_packages(self):
        result = subprocess.check_output([sys.executable, '-E', '-S', '-B', str(HERE / 'optimize.py'),
                                          '--inspect-dataset'], text=True, timeout=10)
        report = json.loads(result)
        self.assertEqual(report['status'], 'inputs_checked')
        self.assertEqual(report['model_requests'], 0)
        self.assertEqual({group: data['count'] for group, data in report['splits'].items()},
                         {'train': 3, 'validation': 3, 'holdout': 3})
        self.assertNotIn('expected', result)

    def test_exact_body_and_header_roundtrip(self):
        header = '---\r\nname: oncall-alert-triage\r\ndescription: 诊断\r\n---\r\n'
        body = '\n# 正文\n保留末尾空格。  \n\n'
        path = self.root / 'SKILL.md'
        self.assertEqual(write_skill(path, header, body), body_hash(body))
        self.assertEqual(read_skill(path), (header, body))
        self.assertEqual(path.read_bytes(), (header + body).encode())

    def test_scorer_calibration_and_all_cases(self):
        self.assertTrue(scorer_selftest()['passed'])
        for tasks in SPLITS.values():
            for task in tasks:
                self.assertEqual(business_grader(task, constructed_result(task, '正文'))['score'], 1)

    def test_reflection_keeps_wrong_cluster_call_and_receipt(self):
        task = SPLITS['train'][1]
        def wrong_run(*, task, skill_body):
            result = constructed_result(task, skill_body)
            messages = result['execution']['messages']
            messages[2]['tool_calls'][0]['function']['arguments'] = json.dumps(
                {'command': './incidentctl alerts all all'})
            messages[3]['content'] = json.dumps({'exit_code': 0, 'output': json.dumps(
                query(task['snapshot'], 'alerts', 'all', 'all'))})
            result['tool_calls'] = pairs(messages)
            return result
        adapter = OncallAdapter(wrong_run)
        candidate = {'skill_body': '正文'}
        batch = adapter.evaluate([task], candidate, capture_traces=True)
        self.assertEqual(batch.scores, [0.0])
        material = adapter.make_reflective_dataset(candidate, batch, ['skill_body'])
        serialized = json.dumps(material, ensure_ascii=False)
        self.assertIn('./incidentctl alerts all all', serialized)
        self.assertIn('staging-eu', serialized)
        self.assertIn('查询保留集群过滤', serialized)
        batch.trajectories[0]['split'] = 'holdout'
        with self.assertRaises(ValueError):
            adapter.make_reflective_dataset(candidate, batch, ['skill_body'])

    def test_infrastructure_failure_not_zero_score(self):
        task = SPLITS['train'][0]
        result = constructed_result(task, '正文')
        result['execution']['infrastructure_error'] = '构造连接失败'
        with self.assertRaises(RuntimeError):
            business_grader(task, result)
        result = constructed_result(task, '正文')
        result['execution']['final_response'] = '无法解析'
        self.assertEqual(business_grader(task, result)['score'], 0)

    def test_output_feedback_does_not_change_original_grading(self):
        task = SPLITS['train'][0]
        result = constructed_result(task, '正文')
        answer = result['execution']['final_response']
        missing_field = json.loads(answer)
        del missing_field['explanation']
        cases = [
            ('纯 JSON', answer, 1.0, [], False),
            ('单个 JSON 块', '```json\n' + answer + '\n```', 1.0, ['format_pass'], False),
            ('块外说明', '调查已完成。\n```json\n' + answer + '\n```', 0.0,
             ['format_pass', 'needs_review'], True),
            ('字段缺失', json.dumps(missing_field), 0.0, ['schema_pass', 'format_pass'], False),
            ('无法解析', '无法解析', 0.0, ['format_pass', 'needs_review'], True),
        ]
        for name, response, score, failed, needs_review in cases:
            with self.subTest(name=name):
                result['execution']['final_response'] = response
                original = grade(task, result['execution'])
                preserved = deepcopy(original)
                with patch('adapter.grade', return_value=original):
                    graded = business_grader(task, result)
                self.assertEqual(original, preserved)
                self.assertEqual(graded['feedback']['grading'], preserved)
                self.assertEqual(graded['score'], score)
                self.assertEqual(graded['feedback']['failed_checks'], failed)
                self.assertEqual(graded['feedback']['needs_review'], needs_review)
                if failed:
                    self.assertTrue(graded['feedback']['reason'])
                if needs_review:
                    self.assertIn('人工复核', graded['feedback']['reason'])
                if name == '块外说明':
                    self.assertIn('业务字段与工具依据检查均通过', graded['feedback']['reason'])
                if original.get('reason'):
                    self.assertIn(original['reason'], graded['feedback']['reason'])

    def test_hash_mismatch_stops_before_scoring(self):
        adapter = OncallAdapter(lambda **kwargs: {'skill_body_sha256': 'wrong'})
        with self.assertRaises(RuntimeError):
            adapter.evaluate([SPLITS['train'][0]], {'skill_body': '正文'})

    def test_official_gepa_neutral_candidate_keeps_baseline(self):
        # 这里的执行器和反思回答均是测试替身，不启动 Hermes 或模型。
        calls = []
        def run_case(*, task, skill_body):
            calls.append((task['group'], skill_body))
            return constructed_result(task, skill_body)
        def fail_score(task, result):
            return {'score': 0.0, 'feedback': '构造等分测试'}
        adapter = OncallAdapter(run_case, fail_score)
        result = gepa.optimize(seed_candidate={'skill_body': 'baseline'},
            trainset=SPLITS['train'], valset=SPLITS['validation'], adapter=adapter,
            reflection_lm=lambda prompt: '```\n不同正文\n```',
            reflection_minibatch_size=1, max_metric_calls=7,
            run_dir=str(self.root / 'gepa'), seed=20)
        self.assertEqual(select_candidate(result, 'baseline'), ('baseline', False))
        self.assertEqual(len(result.candidates), 1)
        self.assertNotIn('holdout', {split for split, _ in calls})

    def test_proxy_configuration_has_no_default(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                connection()
        with patch.dict(os.environ, {'GLM_API_KEY': 'test-only', 'GLM_MODEL': 'glm-test',
                                     'GLM_BASE_URL': 'http://127.0.0.1:9000/v1'}):
            self.assertEqual(connection(), ('glm-test', 'http://127.0.0.1:9000/v1'))
            model = ReflectionLM('glm-test', 'http://127.0.0.1:9000/v1', self.root, 0)
            with patch('urllib.request.build_opener') as opener:
                with self.assertRaises(RuntimeError):
                    model('不会发送')
                opener.assert_not_called()

    def test_official_search_exports_only_strict_improvement(self):
        calls = []
        def run_case(*, task, skill_body):
            calls.append(task['group'])
            result = constructed_result(task, skill_body)
            if skill_body == 'baseline':
                answer = json.loads(result['execution']['final_response'])
                answer['root_cause'] = '构造错误根因'
                result['execution']['final_response'] = json.dumps(answer)
                result['report'] = result['execution']['final_response']
            return result
        result = search(OncallAdapter(run_case), 'baseline',
                        lambda prompt: '```\n构造改进正文\n```', self.root, 8)
        body, improved = select_candidate(result, 'baseline')
        self.assertTrue(improved)
        self.assertEqual(body, result.best_candidate['skill_body'])
        self.assertNotIn('holdout', calls)
        header, _ = read_skill(HERE / 'skills/oncall-alert-triage/SKILL.md')
        path = self.root / 'export/SKILL.md'
        write_skill(path, header, body)
        self.assertEqual(read_skill(path), (header, result.best_candidate['skill_body']))

    def test_one_round_reflects_even_when_perfect_then_stops(self):
        calls, reflections = [], []
        def run_case(*, task, skill_body):
            calls.append((task['id'], task['group']))
            return constructed_result(task, skill_body)
        def reflect(prompt):
            reflections.append(prompt)
            return '```\n构造等分正文\n```'
        result = search(OncallAdapter(run_case), 'baseline', reflect, self.root, 8,
                        iterations=1, minibatch=1)
        self.assertEqual(len(reflections), 1)
        self.assertEqual(len(calls), 5)  # 验证3次，父候选与新候选各1次。
        self.assertEqual(calls[-1], calls[-2])
        self.assertEqual(select_candidate(result, 'baseline'), ('baseline', False))

    def test_runner_compares_actual_skill_receipt(self):
        from support import save, tree
        header, body = read_skill(HERE / 'skills/oncall-alert-triage/SKILL.md')
        runner = HermesRunner(self.root, header, {}, 2)
        def local_worker(work, protocol, forbidden):
            execution = constructed_result(SPLITS['train'][0], body)['execution']
            execution['skill_hashes'] = tree(work / 'skills')
            execution['messages'][1]['content'] = json.dumps({'success': True, 'content': header + body})
            save(work / 'home/execution.json', execution)
            return subprocess.CompletedProcess([], 0, '', '')
        with patch('runner.launch', side_effect=local_worker):
            result = runner(task=SPLITS['train'][0], skill_body=body)
            self.assertEqual(result['skill_body_sha256'], body_hash(body))
        def altered_worker(work, protocol, forbidden):
            response = local_worker(work, protocol, forbidden)
            path = work / 'home/execution.json'
            execution = json.loads(path.read_text())
            execution['messages'][1]['content'] = json.dumps({'success': True, 'content': '被改变的正文'})
            save(path, execution)
            return response
        with patch('runner.launch', side_effect=altered_worker):
            with self.assertRaisesRegex(RuntimeError, 'skill_view'):
                runner(task=SPLITS['train'][0], skill_body=body)

    def test_hermes_session_budget_blocks_launch(self):
        runner = HermesRunner.__new__(HermesRunner)
        runner.session_limit = runner.calls = 1
        runner.phase = 'search'
        with patch('runner.launch') as launch:
            with self.assertRaises(RuntimeError):
                runner(task=SPLITS['train'][0], skill_body='正文')
            with self.assertRaises(ValueError):
                runner(task=SPLITS['holdout'][0], skill_body='正文')
            launch.assert_not_called()

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').is_file(), '真实隔离检查需要 macOS')
    def test_actual_sandbox_denies_answers_without_model(self):
        from isolation import prepare
        source = Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent')).resolve()
        python = Path(os.environ.get('HERMES_PYTHON', source / '.venv/bin/python')).absolute()
        if not python.is_file():
            self.skipTest('没有本地 Hermes 解释器，未验证隔离')
        work = self.root / 'sandbox'
        work.mkdir()
        (work / 'snapshot.json').write_text('{}')
        header, _ = read_skill(HERE / 'skills/oncall-alert-triage/SKILL.md')
        forbidden = HermesRunner(self.root, header, {}, 1).forbidden
        (self.root / 'dataset.json').write_text('{}')
        _, profile = prepare(work, python, source, forbidden)
        code = ('from pathlib import Path\n'
                f'assert Path({str(work / "snapshot.json")!r}).read_text() == "{{}}"\n'
                f'for path in {list(map(str, forbidden))!r}:\n'
                ' try: list(Path(path).iterdir()) if Path(path).is_dir() else Path(path).read_bytes()\n'
                ' except PermissionError: pass\n'
                ' else: raise AssertionError("答案可读")\n')
        process = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(python), '-B', '-c', code],
                                 cwd=work, text=True, capture_output=True, timeout=20,
                                 env={'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == '__main__':
    unittest.main()

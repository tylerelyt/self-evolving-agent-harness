"""Run official GEPA over lesson 23's frozen connection-pool investigations.

The default reflection model is a local protocol fixture. Hermes task execution,
GEPA acceptance and Pareto selection still run through their native interfaces.
"""
from __future__ import annotations

from importlib.metadata import version
import hashlib
import json
from pathlib import Path
import re

from scenario import METHOD, SKILL, TOOLS, score

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / 'fixtures/gepa-tasks.json'
SPLITS = ('train', 'validation', 'holdout')


def digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def load_dataset(path=FIXTURES):
    dataset = json.loads(Path(path).read_text(encoding='utf-8'))
    if dataset.get('schema') != 'lesson23-gepa-tasks-v1':
        raise ValueError('Use this lesson\'s versioned GEPA incident dataset')
    groups = {name: [] for name in SPLITS}
    identities, incidents, scenes = set(), set(), set()
    for task in dataset['tasks']:
        group = task['split']
        if group not in groups or task['case'] not in {'drift', 'upstream'}:
            raise ValueError('Unknown lesson 23 split or task type')
        if (task['id'] in identities or task['incident_id'] in incidents
                or digest({'scope': task['scope'], 'snapshot': task['snapshot']}) in scenes):
            raise ValueError('Incident IDs and frozen scenes must be independent across tasks')
        if (task['scope'].get('service') != 'payment-api' or task['scope'].get('environment') != 'lab'
                or not task['scope'].get('region') or set(task['snapshot']) != set(TOOLS)):
            raise ValueError('GEPA tasks must use this lesson\'s scoped read-only tools')
        identities.add(task['id'])
        incidents.add(task['incident_id'])
        scenes.add(digest({'scope': task['scope'], 'snapshot': task['snapshot']}))
        groups[group].append(task)
    if any(len(tasks) != 2 or {task['case'] for task in tasks} != {'drift', 'upstream'}
           for tasks in groups.values()):
        raise ValueError('Each split needs one distinct drift task and one upstream task')
    return groups, digest(dataset)


def read_skill(path):
    text = Path(path).read_bytes().decode('utf-8')
    match = re.match(r'\A---\r?\n.*?\r?\n---(?:\r?\n|$)', text, re.S)
    if not match or not re.search(r'^name: ' + re.escape(SKILL) + r'\s*$', match.group(), re.M):
        raise ValueError('Use lesson 23\'s timeout-triage Skill and preserve its YAML header')
    return text[:match.end()], text[match.end():]


class LessonAdapter:
    """GEPAAdapter using the assembly's native execution callback and scorer."""
    propose_new_texts = None

    def __init__(self, out, header, execute_case):
        self.out, self.header, self.execute_case = out, header, execute_case
        self.evaluations = []
        self.reflections = []
        self.sessions = set()
        self.reflection_parent = None

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa.core.adapter import EvaluationBatch

        if set(candidate) != {'skill_body'} or not candidate['skill_body'].strip():
            raise ValueError('Only nonempty Skill body text is optimized')
        outputs, scores, trajectories = [], [], []
        for task in batch:
            if task['split'] == 'holdout':
                raise ValueError('Independent holdout tasks cannot enter GEPA search')
            directory = self.out / 'executions' / f'{len(self.evaluations) + 1:04d}'
            skill = directory / 'skills' / SKILL / 'SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_bytes((self.header + candidate['skill_body']).encode('utf-8'))
            expected_sha = file_sha(skill)
            result = self.execute_case(directory / 'task', skill, task)
            trace = result['evidence']
            assessment = score(trace)
            if assessment['status'] not in {'pass', 'fail'}:
                raise RuntimeError('Unjudgeable native execution stops search without assigning a business score')
            if (result['loaded_skill_sha256'] != expected_sha or trace.get('loaded_sha256') != expected_sha
                    or file_sha(skill) != expected_sha):
                raise ValueError('The native task did not read the exact candidate Skill')
            session = result.get('session_id')
            if not isinstance(session, str) or not session or session in self.sessions:
                raise ValueError('Every GEPA evaluation requires an independent native Session')
            self.sessions.add(session)
            snapshot_path = directory / 'task/snapshot.json'
            if json.loads(snapshot_path.read_text()) != task['snapshot']:
                raise ValueError('Native execution ignored the frozen GEPA task snapshot')
            calls = result['calls']
            if any(call['arguments'] != task['scope'] or call['tool'] not in task['snapshot']
                   or call['result'] != task['snapshot'][call['tool']] for call in calls):
                raise ValueError('Native tool receipts differ from this GEPA task\'s scoped scene')
            observed = {call['tool'] for call in calls}
            missing = sorted(set(TOOLS) - observed) if task['case'] == 'drift' else []
            item = {'case_id': task['id'], 'incident_id': task['incident_id'], 'split': task['split'],
                    'case': task['case'], 'session_id': session, 'skill_sha256': expected_sha,
                    'skill_body_sha256': hashlib.sha256(candidate['skill_body'].encode()).hexdigest(),
                    'snapshot_sha256': digest(task['snapshot']), 'tool_calls': calls,
                    'answer': result['answer'], 'evidence': trace, 'score': int(assessment['passed']),
                    'feedback': {'missing_tools': missing, 'assessment': trace.get('assessment'),
                                 'required_condition': 'Compare configured and effective pool limits only after health and upstream checks are normal; keep upstream exceptions on their original branch.'},
                    'artifact_dir': str(directory.relative_to(self.out))}
            self.evaluations.append(item)
            save(directory / 'evaluation.json', item)
            save(self.out / 'evaluations.json', self.evaluations)
            outputs.append(result['answer'])
            scores.append(float(assessment['passed']))
            trajectories.append(item)
        return EvaluationBatch(outputs=outputs, scores=scores,
                               trajectories=trajectories if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        if components_to_update != ['skill_body'] or not eval_batch.trajectories:
            raise ValueError('Reflection requires this candidate\'s actual training trajectories')
        expected_body = hashlib.sha256(candidate['skill_body'].encode()).hexdigest()
        if any(item['split'] != 'train' or item['skill_body_sha256'] != expected_body
               for item in eval_batch.trajectories):
            raise ValueError('Only this parent\'s training evidence may guide a mutation')
        material = {'skill_body': [
            {'Inputs': {'case_id': item['case_id'], 'incident_id': item['incident_id']},
             'Generated Outputs': {'answer': item['answer'], 'tool_calls': item['tool_calls']},
             'Feedback': item['feedback'], 'score': item['score'], 'session_id': item['session_id']}
            for item in eval_batch.trajectories]}
        self.reflection_parent = candidate['skill_body']
        self.reflections.append(material)
        save(self.out / 'reflection-datasets.json', self.reflections)
        return material


class FixtureReflection:
    """Local model fixture responding to the official GEPA reflection prompt."""
    def __init__(self, adapter):
        self.adapter, self.calls = adapter, 0

    def __call__(self, prompt):
        if self.calls:
            raise RuntimeError('This bounded workshop allows one reflection request')
        self.calls += 1
        parent = self.adapter.reflection_parent
        material = self.adapter.reflections[-1]
        prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
        if parent not in prompt_text or any(item['Inputs']['case_id'] not in prompt_text for item in material['skill_body']):
            raise ValueError('The official reflection prompt lost its parent or training evidence')
        failures = [item for item in material['skill_body'] if item['score'] == 0]
        required = {'query_pool_config', 'query_pool_effective'}
        supported = False
        for item in failures:
            observed = {call['tool']: call['result'] for call in item['Generated Outputs']['tool_calls']}
            supported |= (required <= set(item['Feedback']['missing_tools'])
                          and observed.get('query_service_health', {}).get('healthy') is True
                          and observed.get('query_upstream_latency', {}).get('p95_ms', 1001) <= 1000)
        response = parent
        if supported:
            response = re.sub(r'(?m)^3\. .*$',
                '3. 健康与上游检查均正常、超时仍然存在时，执行下面的配置与生效值对照，再报告已取得的观测。',
                parent, count=1)
            response += ('\n\n## ' + METHOD + '\n\n'
                         '只有服务健康且上游 P95 不大于 1000 毫秒时，先调用 query_pool_config 查询配置文件的连接池上限，'
                         '再调用 query_pool_effective 查询实例实际生效值、活跃连接与请求等待。比较两份回执，记录差异，'
                         '将配置漂移与故障的关联留给后续调查。上游异常时保留原有分支，不进行此项检查。'
                         '所有结论只引用实际工具回执，不能把数值差异直接写成根因。\n')
        fenced = '```\n' + response + '\n```'
        save(self.adapter.out / 'reflection' / '001.json', {
            'model': 'lesson23-local-reflection-fixture', 'scripted_model': True,
            'prompt': prompt, 'training_material': material, 'supported_by_missing_checks': supported,
            'response': fenced})
        return fenced


def search(out, baseline, execute_case):
    """Return the officially selected Skill file and provenance for the common gate.

    execute_case(directory, skill_path, frozen_task) must execute a fresh native
    Hermes task and return its recorded result, including evidence and calls.
    Holdout tasks are saved for the assembly to evaluate after this function.
    """
    import gepa

    if version('gepa') != '0.0.27':
        raise RuntimeError('This lesson pins official gepa==0.0.27')
    out, baseline = Path(out).resolve(), Path(baseline).resolve()
    out.mkdir(parents=True, exist_ok=False)
    baseline_sha = file_sha(baseline)
    header, body = read_skill(baseline)
    splits, dataset_sha = load_dataset()
    save(out / 'dataset.json', {'dataset_sha256': dataset_sha, 'splits': splits})
    save(out / 'protocol.json', {'gepa': version('gepa'), 'candidate_selection': 'pareto',
         'baseline_sha256': baseline_sha, 'dataset_sha256': dataset_sha,
         'adapter_sha256': file_sha(Path(__file__)), 'scorer_sha256': file_sha(HERE / 'scenario.py'),
         'reflection_model': 'lesson23-local-reflection-fixture', 'max_metric_calls': 8,
         'reflection_minibatch_size': 2, 'iterations': 1, 'holdout_used_in_search': False})
    adapter = LessonAdapter(out, header, execute_case)
    reflection = FixtureReflection(adapter)
    try:
        result = gepa.optimize(
            seed_candidate={'skill_body': body}, trainset=splits['train'], valset=splits['validation'],
            adapter=adapter, reflection_lm=reflection, candidate_selection_strategy='pareto',
            reflection_minibatch_size=2, skip_perfect_score=False, max_metric_calls=8,
            stop_callbacks=lambda state: state.i + 1 >= 1, use_merge=False,
            cache_evaluation=False, raise_on_exception=True, run_dir=str(out / 'optimizer'), seed=23,
        )
    except BaseException as exc:
        save(out / 'failure.json', {'status': 'incomplete', 'error': type(exc).__name__ + ': ' + str(exc),
                                   'completed_sessions': sorted(adapter.sessions)})
        raise
    save(out / 'search-result.json', result.to_dict())
    selected_body = result.best_candidate['skill_body']
    improved = result.val_aggregate_scores[result.best_idx] > result.val_aggregate_scores[0]
    if not improved:
        selected_body = body
    selected = out / 'selected' / SKILL / 'SKILL.md'
    selected.parent.mkdir(parents=True)
    selected.write_bytes((header + selected_body).encode('utf-8'))
    if file_sha(baseline) != baseline_sha:
        raise ValueError('The formal baseline changed during isolated GEPA search')
    provenance = {'dataset_sha256': dataset_sha, 'optimizer_run': str(out),
                  'dataset_file': str(out / 'dataset.json'), 'source_sessions': sorted(adapter.sessions),
                  'baseline_sha256': baseline_sha, 'candidate_sha256': file_sha(selected),
                  'official_best_idx': result.best_idx, 'selected_idx': result.best_idx if improved else 0,
                  'validation_improved': improved,
                  'reflection_requests': reflection.calls, 'gepa_metric_calls': result.total_metric_calls,
                  'scripted_reflection_model': True, 'holdout_evaluated': False}
    save(out / 'selection.json', provenance)
    return selected, provenance

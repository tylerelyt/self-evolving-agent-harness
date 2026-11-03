"""直接使用第19讲评测包；子进程隔开两讲的同名模块，不改评分语义。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from support import HERE, digest, save, write_skill

LESSON19 = HERE.parent / '19-enterprise-dataset'


def call_case(pack, *, case=None, skill=None, probe=False, turns=12):
    output = LESSON19 / 'runtime' / ('gepa-' + uuid.uuid4().hex)
    command = [sys.executable, '-B', str(LESSON19 / 'case_runner.py'), '--pack', str(pack), '--out', str(output)]
    if case is None:
        command.append('--validate')
    else:
        command += ['--case', case, '--skill', str(skill), '--turns', str(turns), '--probe' if probe else '--live']
    env = dict(os.environ, LECTURE19_ALLOW_MODEL='1', EVAL_MODEL=os.environ.get('GLM_MODEL', 'glm-5.2'))
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=650)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f'第19讲执行失败（保留于{output}）：{stderr[-2000:]}')
    return json.loads((output / ('validated.json' if case is None else 'result.json')).read_text()), output


def load_manifest(path):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if set(manifest) != {'pack', 'splits'} or set(manifest['splits']) != {'train', 'validation', 'holdout'}:
        raise ValueError('清单必须明确指定pack和train、validation、holdout三组题号')
    pack_path = (path.parent / manifest['pack']).resolve()
    pack, _ = call_case(pack_path)
    cases = {case['id']: case for case in pack['ready']}
    splits, seen_cases, seen_incidents, seen_snapshots, seen_traces = {}, set(), set(), set(), set()
    for group, ids in manifest['splits'].items():
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ValueError('每组必须明确选择至少一道不同事故的ready题，不能复制同一道题凑三组')
        selected = []
        for identity in ids:
            if identity not in cases or identity in seen_cases:
                raise ValueError('题号不是ready项，或在清单中重复')
            case = cases[identity]
            if case['split'] != ('holdout' if group == 'holdout' else 'dev'):
                raise ValueError('不能将原留出题转入搜索，也不能未经重分组把开发题当留出题')
            incident = case['snapshot']['incident_id']
            snapshot = case['snapshot']['snapshot_id']
            traces = set(case['provenance']['trace_ids'])
            if incident in seen_incidents or snapshot in seen_snapshots or traces & seen_traces:
                raise ValueError('同一事故、快照或来源Trace不能跨题重复进入搜索与留出')
            seen_cases.add(identity); seen_incidents.add(incident); seen_snapshots.add(snapshot); seen_traces.update(traces)
            selected.append({**case, 'task': case['input']['prompt'], 'group': group,
                             'pack_path': str(pack_path), 'pack_sha256': pack['pack_sha256']})
        splits[group] = selected
    return splits, {'kind': 'lesson19_pack', 'manifest_sha256': digest(path),
                    'pack_sha256': pack['pack_sha256'], 'versions': pack['versions'],
                    'pending_not_selected': len(pack['pending'])}


def business_grader(task, result):
    verdict = result['grading']
    if verdict['status'] != 'scored' or verdict['score'] is None:
        raise RuntimeError('执行或标注未通过，不能把基础设施问题作为低分继续搜索')
    return {'score': float(verdict['score']), 'feedback': {
        'status': verdict['status'], 'failed_checks': verdict['errors'],
        'expected': task['annotation']['expected_output'],
        'required_citations': task['annotation']['required_evidence'],
        'required_exclusion_evidence': task['annotation']['required_exclusion_evidence'],
        'grading': verdict}}


class EnterpriseRunner:
    def __init__(self, root, header, protocol, session_limit):
        self.root, self.header, self.protocol = root, header, protocol
        self.session_limit, self.calls, self.phase = session_limit, 0, 'search'
        self.sessions, self.format_sessions = 0, 0

    def __call__(self, *, task, skill_body, probe=False):
        if task['group'] == 'holdout' and self.phase != 'holdout':
            raise ValueError('搜索期间不能运行留出题')
        reserved = 0 if probe else 2
        if self.sessions + reserved > self.session_limit:
            raise RuntimeError('达到总Hermes会话上限：每题须预留调查和最多一次格式修正')
        if any(token in skill_body for token in ('\r', '${HERMES_SKILL_DIR}', '${HERMES_SESSION_ID}')):
            raise ValueError('候选包含会改变实际正文的模板变量或换行')
        self.calls += 1
        work = self.root / 'cases' / f'{self.calls:04d}'
        skill = work / 'skills/oncall-investigation/SKILL.md'
        expected = write_skill(skill, self.header, skill_body)
        self.sessions += reserved
        result, output = call_case(task['pack_path'], case=task['id'], skill=skill,
                                  probe=probe, turns=self.protocol['max_iterations'])
        execution = result['execution']
        format_sessions = int(bool(execution.get('format_repair')))
        actual = 0 if probe else 1 + format_sessions
        self.sessions -= reserved - actual
        self.format_sessions += format_sessions
        if result['pack_sha256'] != task['pack_sha256'] or execution['skill_sha256'] != digest(skill):
            raise RuntimeError('评测过程中题包或候选内容发生变化')
        value = {'report': execution['final_message'], 'execution': execution, 'grading': result['grading'],
            'tool_calls': [{'name': 'monitor_query', 'args': r['query'], 'receipt': r} for r in execution['receipts']],
            'skill_body_sha256': expected, 'skill_file_sha256': digest(skill),
            'artifact_dir': str(work.relative_to(self.root)), 'lesson19_execution': str(output),
            'case_id': task['id'], 'split': task['group'], 'phase': self.phase}
        save(work / 'result.json', value)
        return value

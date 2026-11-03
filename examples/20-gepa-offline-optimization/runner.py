"""逐候选逐题创建 Home，复用第18讲沙箱、真实 AIAgent 和 SessionDB。"""
import json
import os
import shlex
import time
from pathlib import Path

from support import HERE, LESSON18, digest, read_skill, save, tree, verify_dependency, write_skill
from dataset import public_input
from alert_scorer import pairs
from isolation import launch


class HermesRunner:
    def __init__(self, root, header, protocol, session_limit):
        self.root, self.header, self.protocol = root, header, protocol
        self.session_limit, self.calls = session_limit, 0
        self.phase = 'search'
        self.source = Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent')).resolve()
        verify_dependency(self.source)
        self.python = Path(os.environ.get('HERMES_PYTHON', self.source / '.venv/bin/python')).absolute()
        if not self.python.is_file():
            raise RuntimeError('缺少 Hermes Python 环境，请先按根 README 准备依赖')
        self.forbidden = [HERE / name for name in ('dataset.py', 'adapter.py', 'test_offline.py', 'skills')]
        self.forbidden += [LESSON18 / name for name in ('alert_dataset.py', 'alert_scorer.py', 'alert-skills')]
        # 第18讲工作进程用实际读取探测隔离；预建目录，避免缺文件被误判为执行故障。
        for name in ('gepa', 'reflection', 'export'):
            (root / name).mkdir(parents=True, exist_ok=True)
        self.forbidden += [root / 'dataset.json', root / 'gepa', root / 'reflection', root / 'export']

    def __call__(self, *, task, skill_body):
        if task['group'] == 'holdout' and self.phase != 'holdout':
            raise ValueError('搜索期间不得执行留出题')
        if self.calls >= self.session_limit:
            raise RuntimeError('达到总 Hermes 会话上限；保留记录并停止，不生成优化成功结论')
        if any(token in skill_body for token in ('\r', '${HERMES_SKILL_DIR}', '${HERMES_SESSION_ID}')):
            raise ValueError('候选不能包含会被 Hermes 转换的换行或模板变量；不静默重写正文')
        self.calls += 1
        work = self.root / 'cases' / f'{self.calls:04d}-{task["id"]}'
        work.mkdir(parents=True)
        skill = work / 'skills/oncall-alert-triage/SKILL.md'
        expected_hash = write_skill(skill, self.header, skill_body)
        save(work / 'input.json', public_input(task))
        save(work / 'snapshot.json', task['snapshot'])
        command = work / 'incidentctl'
        command.write_text('#!/bin/sh\nexec ' + shlex.quote(str(self.python)) + ' -B '
                           + shlex.quote(str(LESSON18 / 'incidentctl.py')) + ' "$@"\n')
        command.chmod(0o700)
        before = tree(work / 'skills')
        started = time.monotonic()
        try:
            process = launch(work, self.protocol, self.forbidden)
            save(work / 'process.log', process.stdout + process.stderr)
            output = work / 'home/execution.json'
            execution = json.loads(output.read_text()) if output.exists() else {'infrastructure_error': '没有执行结果'}
            if process.returncode:
                execution['infrastructure_error'] = execution.get('infrastructure_error') or 'Hermes 进程异常退出'
            if tree(work / 'skills') != before or read_skill(skill) != (self.header, skill_body):
                raise RuntimeError('候选 Skill 被执行过程改写')
            if not execution.get('infrastructure_error') and execution.get('skill_hashes') != before:
                raise RuntimeError('Hermes 读取的 Skill 文件与写入文件不同')
            calls = pairs(execution.get('messages', []))
            for call in calls:
                if (call['name'] == 'skill_view' and isinstance(call['args'], dict)
                        and not call['args'].get('file_path') and isinstance(call['receipt'], dict)
                        and call['receipt'].get('success')
                        and call['receipt'].get('content') != self.header + skill_body):
                    raise RuntimeError('skill_view 实际回执与候选文件不同，拒绝比较分数')
            result = {'report': execution.get('final_response') or '',
                      'tool_calls': calls, 'execution': execution,
                      'skill_body_sha256': expected_hash, 'skill_file_sha256': digest(skill),
                      'artifact_dir': str(work.relative_to(self.root)),
                      'case_id': task['id'], 'split': task['group'], 'phase': self.phase,
                      'snapshot_sha256': digest(work / 'snapshot.json'),
                      'elapsed_seconds': round(time.monotonic() - started, 3)}
            save(work / 'result.json', result)
            return result
        except BaseException as exc:
            save(work / 'failure.json', {'status': 'incomplete', 'error': type(exc).__name__ + ': ' + str(exc)})
            raise

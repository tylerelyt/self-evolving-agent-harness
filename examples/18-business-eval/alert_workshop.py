"""当前第18讲的顺序配对评测；每次只保留一个可取消的执行进程。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import time

from common import HERE, REPO, connection_config, credentials, digest, save, source_hashes, tree, verify_dependency
from alert_dataset import CASES, public_input
from alert_scorer import grade
from alert_selftest import run as selfcheck
from isolation import launch


def compare(rows, candidate, repeats):
    keys = [(r['version'], r['case_id'], r['repeat']) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('重复结果不能进入版本比较')
    changes = []
    for item in CASES:
        for repeat in range(1, repeats + 1):
            pair = [next((r for r in rows if (r['version'], r['case_id'], r['repeat']) == (v, item['id'], repeat)), None)
                    for v in ('before', candidate)]
            comparable = all(pair) and all(pair[0].get(k) is not None and pair[0][k] == pair[1].get(k)
                for k in ('input_sha256', 'snapshot_sha256', 'scorer_sha256', 'expected_sha256', 'protocol_sha256', 'model_config'))
            scores = [r['grading']['score'] if r and comparable else None for r in pair]
            values = [None if s is None else s['overall_pass'] for s in scores]
            change = ('无法比较' if None in values else '退步' if values == [True, False]
                      else '改善' if values == [False, True] else '本次相同')
            changes.append({'case_id': item['id'], 'repeat': repeat, 'group': item['group'], 'change': change,
                            'before_pass': values[0], 'candidate_pass': values[1],
                            'content_pass': [None if s is None else s.get('content_pass') for s in scores],
                            'pure_json_format_pass': [None if s is None else s.get('format_pass') for s in scores]})
    recommendation = ('评测未完成或有待人工复核的解释，不能自动决定采用' if any(c['change'] == '无法比较' for c in changes)
                      else '拒绝采用，关键旧任务出现退步' if any(c['change'] == '退步' for c in changes)
                      else '候选仍有未通过任务，继续修订' if any(c['candidate_pass'] is False for c in changes)
                      else '可继续验证；采用前还须核对成本和解释' if any(c['change'] == '改善' for c in changes)
                      else '未观察到收益，保留原版')
    return {'candidate': candidate, 'comparisons': changes, 'recommendation': recommendation, 'production_promoted': False}


class AlertEvaluation:
    def __init__(self):
        self.protocol = json.loads((HERE / 'alert_protocol.json').read_text())
        self.protocol['connection'] = connection_config(self.protocol)
        self.source = Path(os.environ.get('HERMES_SRC', REPO / '.deps/hermes-agent')).resolve()
        self.protocol['hermes_commit'] = verify_dependency(self.source)
        self.protocol['source_hashes'] = source_hashes()
        self.root = HERE / 'runtime' / ('alerts-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        self.root.mkdir(parents=True, mode=0o700)
        self.rows = []; self.prompts = None
        save(self.root / 'protocol.json', self.protocol)
        save(self.root / 'expected.json', {c['id']: c['expected'] for c in CASES})
        self.forbidden = [HERE / 'alert_dataset.py', HERE / 'alert_scorer.py', HERE / 'alert_selftest.py',
                          HERE / 'validation', HERE / 'fixtures', HERE / 'alert-skills', self.root / 'expected.json']
        self.controls = selfcheck(); save(self.root / 'scorer-selftest.json', self.controls)

    def execute(self, version, item, repeat):
        if version not in self.protocol['versions'] or repeat not in range(1, self.protocol['repeats'] + 1):
            raise ValueError('版本或重复编号不在本协议内')
        if any((r['version'], r['case_id'], r['repeat']) == (version, item['id'], repeat) for r in self.rows):
            raise ValueError('本项已执行，请开始新的运行，不覆盖原记录')
        if self.prompts is None:
            self.prompts = credentials()
        work = self.root / version / item['id'] / str(repeat); work.mkdir(parents=True)
        save(work / 'input.json', public_input(item)); save(work / 'snapshot.json', item['snapshot'])
        shutil.copytree(HERE / 'alert-skills' / version, work / 'skills')
        python = Path(os.environ.get('HERMES_PYTHON', self.source / '.venv/bin/python')).absolute()
        command = work / 'incidentctl'
        command.write_text('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' -B ' + shlex.quote(str(HERE / 'incidentctl.py')) + ' "$@"\n')
        command.chmod(0o700)
        started = time.monotonic()
        try:
            process = launch(work, self.protocol, self.forbidden)
            save(work / 'process.log', process.stdout + process.stderr)
            output = work / 'home/execution.json'
            execution = json.loads(output.read_text()) if output.exists() else {'infrastructure_error': '执行进程没有结果'}
            if process.returncode:
                execution['infrastructure_error'] = execution.get('infrastructure_error') or '执行进程异常退出'
        except KeyboardInterrupt:
            save(work / 'interrupted.json', {'interrupted': True, 'score': None})
            raise
        except Exception as exc:
            execution = {'infrastructure_error': type(exc).__name__ + ': ' + str(exc)}
        grading = grade(item, execution)
        row = {'version': version, 'case_id': item['id'], 'repeat': repeat, 'group': item['group'],
               'input_sha256': digest(work / 'input.json'), 'snapshot_sha256': digest(work / 'snapshot.json'),
               'scorer_sha256': hashlib.sha256(json.dumps({n: digest(HERE / n) for n in
                   ('alert_scorer.py', 'incidentctl.py')}, sort_keys=True).encode()).hexdigest(),
               'expected_sha256': hashlib.sha256(json.dumps({'expected': item['expected'], 'required_citations': item['required_citations']}, sort_keys=True).encode()).hexdigest(),
               'protocol_sha256': digest(HERE / 'alert_protocol.json'),
               'model_config': {k: self.protocol[k] for k in ('model', 'temperature', 'connection', 'preset_memory')},
               'skill_hashes': tree(work / 'skills'), 'elapsed_seconds': round(time.monotonic() - started, 3),
               'tool_calls': len([m for m in execution.get('messages', []) if m.get('role') == 'tool']),
               'execution': execution, 'grading': grading}
        self.rows.append(row); save(work / 'result.json', row); save(self.root / 'rows.json', self.rows)
        return row

    def finish(self):
        expected = len(CASES) * len(self.protocol['versions']) * self.protocol['repeats']
        comparisons = {v: compare(self.rows, v, self.protocol['repeats']) for v in ('after', 'known-bad')}
        checks = {'全部执行行留存': len(self.rows) == expected,
                  '本轮源码未变': source_hashes() == self.protocol['source_hashes'],
                  '完整目录未变': all(r['execution'].get('skills_unchanged') and r['skill_hashes'] == r['execution'].get('skill_hashes') for r in self.rows),
                  '评分材料读探测被拒': all(len(r['execution'].get('isolation_probes', [])) == len(self.forbidden)
                      and all(p['denied'] for p in r['execution']['isolation_probes']) for r in self.rows),
                  '所有任务取得可评分结果': all(r['grading']['score'] is not None for r in self.rows),
                  '连接配置一致': all(r['execution'].get('base_url') == self.protocol['connection']['execution_endpoint'] for r in self.rows),
                  'SessionDB关闭重开一致': all(r['execution'].get('session_reopen_verified') for r in self.rows)}
        result = {'checks': checks, 'execution_complete': all(checks.values()), 'comparisons': comparisons,
                  'connection': self.protocol['connection'], 'model_requests': sum(r['execution'].get('api_requests', 0) for r in self.rows),
                  'constructed': True, 'production_promoted': False}
        save(self.root / 'result.json', result)
        save(self.root / 'REPORT.md', '# 告警诊断对照评测\n\n[协议](protocol.json) · [逐题记录](rows.json) · [评分器校准](scorer-selftest.json)\n\n'
             + '\n\n'.join(v + '：' + report['recommendation'] for v, report in comparisons.items())
             + '\n\n[执行与比较汇总](result.json)\n')
        print('记录：', self.root / 'REPORT.md')
        print(comparisons['after']['recommendation'])
        if not result['execution_complete']:
            raise RuntimeError('评测执行不完整；已保存失败，不输出完整评测通过')
        return result


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--offline', action='store_true'); args = parser.parse_args()
    if args.offline:
        print(json.dumps(selfcheck(), ensure_ascii=False, indent=2)); return
    evaluation = AlertEvaluation()
    for repeat in range(1, evaluation.protocol['repeats'] + 1):
        for item in CASES:
            for version in evaluation.protocol['versions']:
                row = evaluation.execute(version, item, repeat)
                print(version, item['id'], repeat, row['grading']['status'], row['grading']['score'])
    evaluation.finish()


if __name__ == '__main__':
    main()

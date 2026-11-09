#!/usr/bin/env python3
"""用构造快照和固定答案复盘一次支付查询的采用决定。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / 'fixtures.json'
BASELINE = '''---
name: payment-status-investigation
description: 只读查询付款状态和实际到账金额。
---

核对订单和全部付款流水。只将最终到账的款项计入成功金额。
渠道受理与最终到账分别报告。没有规则依据时将内部队列标为未知。
'''
QUEUE = 'payops-confirm-17'
NEW_RULE = f'最终状态为处理中且查询回执明确标注渠道已受理时，交给 {QUEUE}。\n'
FIELDS = ('amount', 'final_status', 'queue')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(root, phase, item, skill, fixtures):
    # 读取实际 Skill 副本；每次回放独立写盘。回答仍是预置样本。
    arm = 'candidate' if NEW_RULE in skill.read_text(encoding='utf-8') else 'baseline'
    snapshot = fixtures['orders'][item['order']]
    snapshot_bytes = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode('utf-8')
    record = {
        'session': uuid4().hex,
        'case': item['id'],
        'prompt': item['question'],
        'skill_sha256': sha(skill),
        'answer': fixtures['answers'][item[arm]],
        'receipt': {'order_id': item['order'], 'snapshot_sha256': hashlib.sha256(snapshot_bytes).hexdigest(),
                    'result': snapshot},
    }
    path = root / phase / f'{item["id"]}.json'
    write_json(path, record)
    return record, path


def review(root, work_path, baseline):
    # 复盘重新打开落盘的任务记录。规则是教学脚本，不调用模型。
    work = read_json(work_path)
    result = work['receipt']['result']
    supported = (result['final_status'] == '处理中' and result.get('channel_accepted') is True
                 and '内部队列：未知' in work['answer'])
    if not supported:
        raise ValueError('本次经历没有支持教学候选的依据')
    candidate = root / 'methods/candidate/SKILL.md'
    candidate.parent.mkdir(parents=True)
    candidate.write_text(baseline.read_text(encoding='utf-8') + NEW_RULE, encoding='utf-8')
    write_json(root / 'review.json', {'experience': str(work_path.relative_to(root)),
                                     'experience_sha256': sha(work_path),
                                     'baseline_sha256': sha(baseline), 'candidate_sha256': sha(candidate),
                                     'rule': NEW_RULE, 'kind': 'teaching_rule'})
    return candidate


def score(answer, expected, judge):
    amount = re.search(r'成功金额：([0-9+ =]+) 元。', answer)
    status = re.search(r'最终状态：(已到账|处理中|部分到账)。', answer)
    queue = re.search(r'内部队列：(未知|[a-z0-9-]+)。', answer)
    if not all((amount, status, queue)):
        return {'pass': None, 'reason': 'unreadable_answer'}
    expression = amount[1]
    terms = re.fullmatch(r'([0-9]+(?: \+ [0-9]+)*)(?: = ([0-9]+))?', expression)
    if not terms:
        return {'pass': None, 'reason': 'unreadable_amount'}

    if judge == 'legacy':
        # 故意保留旧评分器的三个问题，供读者用校准样本定位。
        actual = [int(terms[1].split(' + ')[0]), '已到账' if '渠道已受理' in answer else status[1]]
    else:
        total = sum(map(int, terms[1].split(' + ')))
        if terms[2] is not None and int(terms[2]) != total:
            return {'pass': False, 'reason': 'amount_equation_mismatch'}
        actual = [total, status[1], queue[1]]
    checks = {field: observed == truth for field, observed, truth in zip(FIELDS, actual, expected)}
    return {'pass': all(checks.values()), 'parsed': dict(zip(FIELDS, actual)), 'checks': checks}


def decide(calibration, pairs):
    main_passes = {arm: sum(row[arm]['pass'] is True for row in pairs['main'])
                   for arm in ('baseline', 'candidate')}
    violations = {
        'judge_calibration_failed': any(row['verdict']['pass'] is not row['expected_pass'] for row in calibration),
        'incomparable': any(row[arm]['pass'] is None for group in pairs.values()
                            for row in group for arm in ('baseline', 'candidate')),
        'critical_regression': any(row['baseline']['pass'] and row['candidate']['pass'] is False
                                   for row in pairs['critical']),
        'no_main_gain': main_passes['candidate'] <= main_passes['baseline'],
        'rephrased_regression': any(row['baseline']['pass'] and row['candidate']['pass'] is False
                                    for row in pairs['rephrased']),
    }
    blockers = [reason for reason, triggered in violations.items() if triggered]
    return {'adopt': not blockers, 'blockers': blockers, 'main_passes': main_passes,
            'main_cases': len(pairs['main'])}


def replay(root, judge):
    inputs = root / 'inputs.json'
    shutil.copyfile(FIXTURES, inputs)
    fixtures = read_json(inputs)
    write_json(root / 'protocol.json', {'mode': 'synthetic_fixture_replay', 'judge': judge,
                                      'fixture_sha256': sha(inputs), 'runner_sha256': sha(Path(__file__)),
                                      'adoption_gates': ['judge calibration', 'comparable pairs', 'no critical regression',
                                                         'strict main gain', 'no rephrased regression']})
    baseline = root / 'methods/baseline/SKILL.md'
    baseline.parent.mkdir(parents=True)
    baseline.write_text(BASELINE, encoding='utf-8')
    official = root / 'official.json'
    write_json(official, {'version': 'baseline', 'sha256': sha(baseline)})
    calibration = [{'id': item['id'], 'answer': fixtures['answers'][item['answer']],
                    'expected_pass': item['pass'], 'expected_fields': dict(zip(FIELDS, item['expected'])),
                    'verdict': score(fixtures['answers'][item['answer']], item['expected'], judge)}
                   for item in fixtures['calibration']]
    write_json(root / 'calibration.json', calibration)

    _, work = run_case(root, 'experience', fixtures['experience'], baseline, fixtures)
    candidate = review(root, work, baseline)
    pairs = {}
    for group, cases in fixtures['pairs'].items():
        pairs[group] = []
        for item in cases:
            pair = {'id': item['id'], 'expected_fields': dict(zip(FIELDS, item['expected']))}
            for arm, skill in (('baseline', baseline), ('candidate', candidate)):
                record, path = run_case(root, f'pairs/{group}/{arm}', item, skill, fixtures)
                pair[arm] = {'record': str(path.relative_to(root)), **score(record['answer'], item['expected'], judge)}
            pairs[group].append(pair)
    write_json(root / 'pairs.json', pairs)

    decision = decide(calibration, pairs)
    selected = candidate if decision['adopt'] else baseline
    if decision['adopt']:
        write_json(official, {'version': 'candidate', 'sha256': sha(candidate)})
    write_json(root / 'decision.json', {**decision, 'official': read_json(official)})

    received = {}
    for arm, source in (('official', selected), ('candidate-retest', candidate)):
        copy = root / 'sharing' / arm / 'SKILL.md'
        copy.parent.mkdir(parents=True)
        shutil.copyfile(source, copy)
        if sha(copy) != sha(source):
            raise RuntimeError('接收副本与来源的指纹不同')
        received[arm] = copy
        run_case(root, f'sharing/{arm}', fixtures['sharing'], copy, fixtures)
    recovery = root / 'recovery-rehearsal/SKILL.md'
    recovery.parent.mkdir(parents=True)
    shutil.copyfile(received['candidate-retest'], recovery)
    before = sha(recovery)
    shutil.copyfile(baseline, recovery)
    record, _ = run_case(root, 'recovery-rehearsal', fixtures['sharing'], recovery, fixtures)
    verified = before == sha(candidate) and record['skill_sha256'] == sha(baseline)
    write_json(root / 'recovery-rehearsal/check.json', {'before': before, 'restored': record['skill_sha256'],
                                                       'baseline': sha(baseline), 'official': read_json(official),
                                                       'verified': verified})
    write_json(root / 'status.json', {'completed': True, 'recovery_verified': verified})
    return read_json(root / 'decision.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--judge', choices=('legacy', 'calibrated'), default='legacy')
    parser.add_argument('--out', type=Path, default=HERE / 'runtime' / uuid4().hex[:12],
                        help='尚不存在的输出目录')
    args = parser.parse_args()
    args.out.mkdir(parents=True)
    result = replay(args.out, args.judge)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f'本轮记录目录 {args.out.resolve()}')

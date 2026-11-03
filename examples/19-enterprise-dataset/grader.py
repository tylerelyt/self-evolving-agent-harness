"""评分器运行在宿主侧；独立标注不会进入 Hermes 进程。"""
import argparse
import json
import os
from pathlib import Path

from dataset import read, verify
from monitor import query
from schema import SCORER_VERSION, annotation_errors


def grade(case, execution):
    review = {'scorer_version': SCORER_VERSION, 'explanation_nonempty': None,
              'explanation_semantics': 'not_evaluated'}
    if execution.get('status') != 'completed':
        return {**review, 'status': 'unscored', 'passed': False, 'score': None,
                'errors': ['执行未正常完成；不能将基础设施失败当作业务得分']}
    annotation = case['annotation']
    invalid = annotation_errors(annotation, case['snapshot'], case['id'].split('@')[1])
    if invalid:
        return {**review, 'status': 'unscored', 'passed': False, 'score': None, 'errors': invalid}
    errors = []
    try:
        answer = json.loads(execution['final_message'])
        review['explanation_nonempty'] = isinstance(answer.get('explanation'), str) and bool(answer['explanation'].strip())
        for key, expected in annotation['expected_output'].items():
            if answer.get(key) != expected:
                errors.append(key + ' 不符')
        citations = answer['evidence_ids']
        if not isinstance(citations, list) or any(not isinstance(x, str) for x in citations) or len(citations) != len(set(citations)):
            raise ValueError('证据编号须为不重复的字符串数组')
        observed = set()
        for receipt in execution['receipts']:
            if 'error' in receipt:
                continue
            actual = query(case['snapshot'], **receipt['query'])
            if actual != receipt:
                errors.append('工具回执不属于本题快照')
                continue
            observed.update(r['id'] for r in actual['rows'])
        valid = {r['id'] for r in case['snapshot']['rows']}
        if not set(citations) <= observed & valid:
            errors.append('引用未实际查到或不属于本题快照的证据')
        cited_and_observed = set(citations) & observed
        if not set(annotation['required_evidence']) <= cited_and_observed:
            errors.append('目标集群的根因、链路或影响范围证据未全部查到并引用')
        if not set(annotation['required_exclusion_evidence']) <= cited_and_observed:
            errors.append('排除集群同名服务的必要证据未全部查到并引用')
        if not review['explanation_nonempty']:
            errors.append('缺少因果说明文本（仅检查非空）')
    except (KeyError, ValueError, TypeError, AttributeError):
        errors.append('回答或执行记录格式无效')
    return {**review, 'status': 'scored', 'passed': not errors, 'score': int(not errors), 'errors': errors,
            'explanation_review': '仅检查文本非空；因果解释及引用在文字中的使用是否正确，未作自动语义判断'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pack', type=Path, required=True)
    parser.add_argument('--case', required=True)
    args = parser.parse_args()
    pack = read(args.pack)
    verify(pack)
    case = next(c for c in pack['ready'] if c['id'] == args.case)
    # Engine 在独立子进程退出后才写入工作区；模型没有写入权限。
    execution = read(Path.cwd() / 'execution.json')
    if execution['final_message'] != os.environ.get('EVAL_FINAL_MESSAGE', '') or int(os.environ.get('EVAL_EXIT_CODE', '1')) != 0:
        execution['status'] = 'engine_failed'
    result = grade(case, execution)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()

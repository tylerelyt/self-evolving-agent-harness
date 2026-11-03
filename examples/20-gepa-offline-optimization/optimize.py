"""官方 GEPA 搜索入口；留出评测只发生在候选选定并原样导出之后。"""
import argparse
from importlib.metadata import version
import json
import secrets
import signal
import time
from pathlib import Path

import gepa
from support import HERE, LESSON18, body_hash, digest, read_skill, save, write_skill
from adapter import OncallAdapter
from dataset import SPLITS, validate_splits
from reflection import ReflectionLM, connection
from runner import HermesRunner


def select_candidate(result, baseline):
    if result.candidates[0] != {'skill_body': baseline}:
        raise ValueError('GEPA 起始候选与本次 baseline 不一致')
    optimized_body = result.best_candidate['skill_body']
    improved = (result.val_aggregate_scores[result.best_idx] > result.val_aggregate_scores[0]
                and optimized_body != baseline)
    return (optimized_body if improved else baseline), improved


def execution_protocol(model, endpoint, turns, provenance):
    if provenance['kind'] == 'lesson19_pack':
        protocol = {'protocol_version': 'lesson19-gepa-v2', 'temperature': 0,
                    'max_tokens': 4096, 'worker': '19-enterprise-dataset/worker.py',
                    'worker_timeout_seconds': 600, 'learning_during_evaluation': False,
                    'output_protocol': 'oncall-json-v2', 'response_format': {'type': 'json_object'},
                    'format_repair_limit': 1,
                    'scorer_version': provenance['versions']['scorer_version']}
    else:
        protocol = json.loads((LESSON18 / 'alert_protocol.json').read_text())
    protocol.update(model=model, base_url=endpoint, max_iterations=turns,
                    connection={'execution_endpoint': endpoint})
    return protocol


def search(adapter, baseline, reflection_lm, root, max_metric_calls, iterations=1, minibatch=1, splits=None):
    splits = SPLITS if splits is None else splits
    # 不向 GEPA 提供 holdout；接受检验和 Pareto 选择均使用官方实现。
    return gepa.optimize(
        seed_candidate={'skill_body': baseline},
        trainset=splits['train'],
        valset=splits['validation'],
        adapter=adapter,
        reflection_lm=reflection_lm,
        candidate_selection_strategy='pareto',
        reflection_minibatch_size=minibatch,
        # 最小练习也观察一次反思和候选比较；满分不跳过，但等分仍不接受。
        skip_perfect_score=False,
        max_metric_calls=max_metric_calls,
        # GEPA 0.0.27 的初始 i=-1；完成第一轮后 i=0。
        stop_callbacks=lambda state: state.i + 1 >= iterations,
        use_merge=False,
        cache_evaluation=False,
        raise_on_exception=True,
        run_dir=str(root / 'gepa'),
        seed=20,
    )


def run(args):
    if version('gepa') != '0.0.27':
        raise RuntimeError('本例固定 gepa==0.0.27，拒绝静默换用其他版本')
    from adapter import business_grader
    runner_type = HermesRunner
    if args.dataset_manifest:
        from enterprise import EnterpriseRunner, business_grader, load_manifest
        splits, provenance = load_manifest(args.dataset_manifest)
        runner_type = EnterpriseRunner
        if not args.skill:
            raise ValueError('使用第19讲评测包时必须指定与monitor_query工具配套的 --skill')
    else:
        validate_splits()
        splits, provenance = SPLITS, {'kind': 'constructed_lesson18_cases'}
    if not args.probe and (args.metric_calls <= len(splits['validation'])
                          or min(args.sessions, args.reflections, args.iterations, args.minibatch) < 1):
        raise ValueError('预算必须为正，metric-calls 须大于初始验证题数，才能进入反思')
    model, endpoint = ('probe', 'not_connected') if args.probe else connection()
    seed_skill = args.skill or HERE / 'skills/oncall-alert-triage/SKILL.md'
    header, baseline = read_skill(seed_skill)
    protocol = execution_protocol(model, endpoint, args.turns, provenance)
    root = HERE / 'runtime' / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
    root.mkdir(parents=True, mode=0o700)
    print('本次记录：', root / 'REPORT.md', flush=True)
    save(root / 'REPORT.md', '# 第20讲离线优化记录\n\n状态：进行中，尚无成功结论。\n\n'
         '[配置](config.json) · [固定题库](dataset.json) · [逐题执行](cases/) · [反思](reflection/)\n')
    try:
        runner = runner_type(root, header, protocol, args.sessions)
        source_hashes = {str(p.relative_to(HERE)): digest(p) for p in HERE.glob('*.py')}
        save(root / 'config.json', {'gepa': version('gepa'), 'model': model, 'endpoint': endpoint,
             'upstream_verified': False, 'protocol': protocol, 'source_hashes': source_hashes,
             'lesson18_hashes': {n: digest(LESSON18 / n) for n in
                                ('alert_scorer.py', 'incidentctl.py', 'isolation.py', 'hermes_worker.py')},
             'lesson19_hashes': {n: digest(HERE.parent / '19-enterprise-dataset' / n) for n in
                                ('engine.py', 'worker.py', 'case_runner.py', 'monitor.py', 'grader.py')}
                                if args.dataset_manifest else None,
             'metric_calls': args.metric_calls, 'session_limit': args.sessions,
             'reflection_limit': args.reflections, 'iterations': args.iterations, 'minibatch': args.minibatch,
             'baseline_sha256': body_hash(baseline), 'dataset_source': provenance})
        save(root / 'dataset.json', splits)
        if args.probe:
            result = runner(task=splits['train'][0], skill_body=baseline, probe=True)
            if result['execution']['status'] != 'probe_only':
                raise RuntimeError('Hermes预检未通过')
            save(root / 'probe.json', result)
            save(root / 'REPORT.md', '# 第20讲数据接续预检\n\n第19讲包校验、候选加载、只读工具和答案隔离通过；未调用模型。\n\n[配置](config.json) · [题库](dataset.json) · [实际预检](probe.json)\n')
            return root
        reflection_records = []
        evaluation_records = []
        save(root / 'reflection-datasets.json', reflection_records)
        def record_reflection(material):
            reflection_records.append(material)
            save(root / 'reflection-datasets.json', reflection_records)
        def record_evaluation(trace):
            evaluation_records.append(trace)
            save(root / 'evaluations.json', evaluation_records)
        adapter = OncallAdapter(runner, grade_case=business_grader, record_reflection=record_reflection, record_evaluation=record_evaluation)
        reflection_lm = ReflectionLM(model, endpoint, root / 'reflection', args.reflections)
        result = search(adapter, baseline, reflection_lm, root, args.metric_calls, args.iterations, args.minibatch, splits)
        save(root / 'search-result.json', result.to_dict())
        body, improved = select_candidate(result, baseline)
        export = root / 'export' / Path(seed_skill).parent.name / 'SKILL.md'
        exported_hash = write_skill(export, header, body)
        # 留出评测读取导出的文件，而不是另一个变量或重建过的提示词。
        exported_header, exported_body = read_skill(export)
        if (exported_header, exported_body) != (header, body):
            raise RuntimeError('导出与选择的候选不同')
        runner.phase = 'holdout'
        before = adapter.evaluate(splits['holdout'], {'skill_body': baseline})
        after = adapter.evaluate(splits['holdout'], {'skill_body': exported_body}) if improved else before
        regressions = [task['id'] for task, old, new in zip(splits['holdout'], before.scores, after.scores)
                       if new < old]
        summary = {'status': 'completed', 'dataset_source': provenance, 'production_promoted': False,
                   'validation_improved': improved, 'selected_idx': result.best_idx if improved else 0,
                   'baseline_body_sha256': body_hash(baseline), 'exported_body_sha256': exported_hash,
                   'exported_skill': str(export.relative_to(root)),
                   'holdout_baseline': before.scores, 'holdout_selected': after.scores,
                   'holdout_reused_identical_baseline': not improved, 'holdout_regressions': regressions,
                   'hermes_sessions': getattr(runner, 'sessions', runner.calls),
                   'investigation_sessions': runner.calls,
                   'format_sessions': getattr(runner, 'format_sessions', 0),
                   'reflection_requests': reflection_lm.calls,
                   'gepa_metric_calls': result.total_metric_calls,
                   'decision': ('未观察到验证集改进，保持 baseline' if not improved else
                                '留出题出现退步，拒绝采用候选' if regressions else
                                '候选供独立审阅；仍须核对逐题依据、解释和成本，不自动上线')}
        save(root / 'summary.json', summary)
        save(root / 'REPORT.md', '# 第20讲离线优化记录\n\n' + summary['decision'] + '\n\n'
             '[配置](config.json) · [固定题库](dataset.json) · [逐题执行](cases/) · [逐题评分](evaluations.json)\n\n'
             '[GEPA 候选与验证分数](search-result.json) · [反思材料](reflection-datasets.json)\n\n'
             f'[独立留出评测汇总](summary.json) · [原样导出的 Skill]({export.relative_to(root)})\n\n'
             '题库来源与版本见配置。搜索未使用留出分数；留出结果不回流修改候选。'
             '无验证改进时只评测一次相同 baseline，不伪造对照收益。\n')
        return root
    except BaseException as exc:
        save(root / 'failure.json', {'status': 'incomplete', 'error': type(exc).__name__ + ': ' + str(exc)})
        save(root / 'REPORT.md', '# 第20讲离线优化记录\n\n状态：未完成，不宣称优化成功。\n\n'
             '[失败原因](failure.json) · [已有逐题记录](cases/) · [运行配置](config.json)\n')
        raise


def main():
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='明确允许本次模型调用')
    parser.add_argument('--probe', action='store_true', help='配合dataset-manifest检查真实Hermes加载与隔离，不调用模型')
    parser.add_argument('--dataset-manifest', type=Path, help='第19讲pack及明确划分的三组题号')
    parser.add_argument('--skill', type=Path, help='本次优化的真实SKILL.md，保留文件头与正文')
    parser.add_argument('--metric-calls', type=int, default=8, help='GEPA 任务评分停止阈值，不是 API 请求数')
    parser.add_argument('--sessions', type=int, default=14, help='含留出评测的总 Hermes 会话硬上限')
    parser.add_argument('--reflections', type=int, default=1, help='反思请求硬上限')
    parser.add_argument('--iterations', type=int, default=1, help='完成指定搜索轮数后进入留出评测，不耗尽预算')
    parser.add_argument('--minibatch', type=int, default=1, help='同批比较父候选与新候选的训练题数')
    parser.add_argument('--turns', type=int, default=16, help='每次 Hermes 会话最多轮数')
    args = parser.parse_args()
    if args.probe and (args.live or not args.dataset_manifest):
        parser.error('--probe需要dataset-manifest且不能与live同时使用')
    if not args.live and not args.probe:
        parser.error('真实运行必须显式传入 --live；无费用检查请运行 bash run.sh --offline')
    if args.turns < 1:
        parser.error('--turns 必须为正')
    run(args)


if __name__ == '__main__':
    main()

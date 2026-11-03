"""真实 Hermes 调查、原生插件采集、数据集版本取回与 Skill Up 新一轮调查。"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import signal
import time
import uuid

from dataset import HERE, build, digest, file_digest, inside, read, save
from engine import execute
from langfuse_io import client, collect, export, map_tasks, publish, restore
from monitor import query
from run import evaluate, materialize


def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def plugin_projection(value, path='$', depth=0):
    """固定 Hermes 插件的四层/五十项裁剪规则；同时保留明确的缺失位置。"""
    if depth > 4:
        return '<max-depth>', [path]
    if isinstance(value, (dict, list)):
        pairs = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
        result = {} if isinstance(value, dict) else []
        omitted = [f'{path}[{k}]' for k, _ in pairs[50:]]
        for key, item in pairs[:50]:
            projected, paths = plugin_projection(item, f'{path}[{key}]', depth + 1)
            if isinstance(result, dict):
                result[key] = projected
            else:
                result.append(projected)
            omitted.extend(paths)
        return result, omitted
    return value, []


def confirm_snapshot_receipts(execution, snapshot):
    if execution['snapshot_content_sha256'] != digest(snapshot):
        raise ValueError('实际调查与固定快照的内容哈希不符')
    for receipt in execution['receipts']:
        args = receipt['query']
        try:
            expected = query(snapshot, **args)
        except (TypeError, KeyError, ValueError) as exc:
            expected = {'error': str(exc), 'query': args}
        if receipt != expected:
            raise ValueError('完整工具回执不能由固定快照按原参数重新得到')


def confirm_native_trace(source, execution):
    """核对服务端真实记录，不通过重新上传本地 execution.json 补齐缺失轨迹。"""
    trace = source['traces'][0]
    observations = trace['raw_trace']['observations']
    tools = [item for item in observations if item['name'] == 'Tool: monitor_query']
    generations = [item for item in observations if item.get('type') == 'GENERATION']
    expected, truncated = Counter(), []
    complete_expected = Counter(digest(item) for item in execution['receipts'])
    for receipt in execution['receipts']:
        projected, omitted = plugin_projection(receipt)
        expected[digest(projected)] += 1
        if omitted:
            truncated.append({'query': receipt['query'], 'paths': omitted})
    actual = Counter(digest(decoded(item.get('output'))) for item in tools)
    if not tools or actual != expected:
        raise ValueError('原生插件记录的工具结果尚未完整入库，或与本次调查不一致')
    if len(generations) != execution.get('investigation_api_calls', execution['api_calls']):
        raise ValueError('原生插件记录的模型调用数与调查不一致')
    for item in generations:
        model_input, model_output = decoded(item.get('input')), decoded(item.get('output'))
        if (not isinstance(model_input, list) or not model_input
                or not isinstance(model_output, dict)
                or not (model_output.get('content') or model_output.get('tool_calls'))):
            raise ValueError('模型调用缺少实际输入或输出，不能称为完整轨迹')
    final_output = decoded(trace.get('output'))
    if not isinstance(final_output, dict) or final_output.get('content') != execution.get('investigation_final_message', execution['final_message']):
        raise ValueError('Trace 最终回答与本轮实际回答不一致')
    if trace['metadata'].get('session_id') not in (None, execution['telemetry']['session_id']):
        raise ValueError('记录属于另一 Session')
    if trace['metadata'].get('task_id') != execution['telemetry']['task_id']:
        raise ValueError('记录属于另一任务')
    if any(not item.get('endTime') for item in observations):
        raise ValueError('存在尚未结束的 Observation')
    return {'model_observations': len(generations), 'tool_observations': len(tools),
            'tool_results_identical': actual == complete_expected,
            'tool_results_match_plugin_projection': True, 'truncated_tool_results': truncated,
            'native_task_id_verified': True, 'model_io_present': True,
            'model_input_last_messages_limit': 12, 'final_response_identical': True}


def wait_for_trace(lf, execution, timeout=90):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            source = collect(lf, [execution['telemetry']['expected_trace_id']])
            check = confirm_native_trace(source, execution)
            repair = execution.get('format_repair')
            if repair:
                formatted = collect(lf, [execution['telemetry']['format_trace_id']])
                trace = formatted['traces'][0]
                observations = trace['raw_trace']['observations']
                generations = [o for o in observations if o.get('type') == 'GENERATION']
                output = decoded(trace.get('output'))
                if (len(generations) != repair['api_calls'] or not isinstance(output, dict)
                        or output.get('content') != repair['corrected_response']
                        or any(o.get('type') == 'TOOL' or o.get('name', '').startswith('Tool:')
                               or not o.get('endTime') for o in observations)):
                    raise ValueError('格式修正的原生 Trace 尚不完整或与实际回答不符')
                for item in generations:
                    model_input, model_output = decoded(item.get('input')), decoded(item.get('output'))
                    if (not isinstance(model_input, list) or not model_input
                            or not isinstance(model_output, dict) or not model_output.get('content')
                            or model_output.get('tool_calls')):
                        raise ValueError('格式修正的模型调用缺少实际输入输出，或使用了工具')
                check['format_repair_trace'] = formatted
            return source, check
        except (LookupError, ValueError) as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError('原生采集未完整到达服务端：' + str(last_error))


def run(output, task_path, annotations, snapshots, skill, dataset_name, probe=False, lf=None, resume=False):
    output.mkdir(parents=True, exist_ok=resume)
    task = read(task_path)
    if set(task) != {'input', 'metadata'}:
        raise ValueError('业务入口只接受 input 与 metadata，不接受历史回答或标准答案')
    snapshot_id = task['metadata']['snapshot_id']
    snapshot_path = snapshots / (snapshot_id + '.json')
    # 在第一轮调查之前绑定独立标注与环境版本，之后不按模型回答修改答案。
    frozen = {'annotation_sha256': file_digest(annotations), 'snapshot_sha256': file_digest(snapshot_path),
              'skill_sha256': file_digest(skill), 'task_sha256': file_digest(task_path)}
    os.environ['LECTURE19_ALLOW_MODEL'] = '1'
    if resume:
        if read(output / 'inputs.json') != frozen:
            raise ValueError('原调查的输入、快照、标注或 Skill 已变化，不能接续')
        execution = read(output / 'investigation.json')
        if not execution.get('model_called') or not execution.get('telemetry'):
            raise ValueError('没有可以接续的原生调查')
        print('1/4 保留已完成的原生调查，只接续服务端采集，不再调用模型调查一次', flush=True)
    else:
        save(output / 'inputs.json', frozen)
        workspace = output / 'investigation'
        workspace.mkdir()
        shutil.copyfile(snapshot_path, workspace / 'snapshot.json')
        print('1/4 Hermes 调查；由原生插件记录执行' if not probe else '检查原生插件与隔离，不调用模型', flush=True)
        execution = execute({'workspace': str(workspace), 'messages': [
            {'role': 'user', 'content': task['input']['prompt']}]}, skill,
            [annotations, HERE / 'grader.py'], probe=probe, observe=True)
        save(output / 'investigation.json', execution)
    if probe:
        if execution['status'] != 'probe_only':
            raise RuntimeError('原生插件或隔离预检失败，详见 investigation.json')
        return {'status': 'probe_only', 'model_called': False}
    if execution['status'] != 'completed':
        raise RuntimeError('调查未正常完成，停止制作评测题，详见 investigation.json')
    confirm_snapshot_receipts(execution, read(snapshot_path))
    print('2/4 从 Langfuse 取回本次调查，核对模型调用与工具结果', flush=True)
    source, observation_check = wait_for_trace(lf, execution)
    save(output / 'collected.json', source)
    recorded_input = source['traces'][0]['input']
    if isinstance(recorded_input, dict) and recorded_input.get('role') == 'user':
        recorded_input = recorded_input.get('content')
    if recorded_input != execution.get('submitted_prompt', task['input']['prompt']):
        raise ValueError('原生记录的任务输入与业务入口不符，不能直接映射')
    trace_id = source['traces'][0]['id']
    task['metadata']['skill_version'] = frozen['skill_sha256']
    mapping = {trace_id: task}
    save(output / 'task-map.json', mapping)
    save(output / 'mapped.json', map_tasks(source, mapping))
    for key, path in [('annotation_sha256', annotations), ('snapshot_sha256', snapshot_path),
                      ('skill_sha256', skill), ('task_sha256', task_path)]:
        if file_digest(path) != frozen[key]:
            raise ValueError('调查后输入版本发生变化：' + key)
    pack = build(output / 'mapped.json', annotations, snapshots, output / 'prepared')
    if len(pack['ready']) != 1 or pack['pending']:
        raise ValueError('本次调查材料不齐，保留在 prepared/pending.json，不进入评测')
    published = publish(lf, dataset_name, pack)
    save(output / 'published.json', published)
    version = published['export_version_hint']
    print('3/4 按固定数据集时间点取回题目，核对快照与独立标注', flush=True)
    exported = export(lf, dataset_name, version)
    save(output / 'exported.json', exported)
    restored = restore(exported, {snapshot_id: read(snapshot_path)}, output / 'restored')
    if len(restored['ready']) != 1 or restored['pending']:
        raise ValueError('按版本取回的数据不完整，不进入评测')
    case = restored['ready'][0]
    original = pack['ready'][0]
    for key in ('input', 'snapshot', 'annotation', 'split', 'scenario'):
        if case[key] != original[key]:
            raise ValueError('取回的版本改变了 ' + key)
    if case['provenance']['source_trace_id'] != trace_id:
        raise ValueError('取回版本失去了原生 Trace 关联')

    evaluation = output / 'evaluation'
    print('4/4 Skill Up 启动另一轮 Hermes 调查；保留实际评分，不重试挑选结果', flush=True)
    materialize(output / 'restored/pack.json', skill, evaluation, task['metadata']['split'])
    returncode = evaluate(evaluation, live=True)
    receipts = list((evaluation / 'receipts').glob('*.json'))
    if len(receipts) != 1:
        raise RuntimeError('没有取得 Skill Up 新一轮调查的唯一执行记录')
    replay = read(receipts[0])
    if (replay['status'] != 'completed' or not replay.get('model_called') or not replay.get('receipts')
            or replay['actor_directory'] == execution['actor_directory']):
        raise RuntimeError('没有完成独立的新调查，不能用旧回答重评分替代')
    if (replay['snapshot_content_sha256'] != execution['snapshot_content_sha256']
            or replay['snapshot_content_sha256'] != digest(case['snapshot'])
            or replay['skill_sha256'] != frozen['skill_sha256']):
        raise RuntimeError('新一轮调查的环境或 Skill 不一致')
    confirm_snapshot_receipts(replay, case['snapshot'])
    native = read(evaluation / 'results/iteration-1/result.json')
    return {'status': 'pipeline_completed', 'collection_resumed': resume, 'native_plugin_verified': observation_check,
            'trace_id': trace_id, 'dataset_name': dataset_name, 'dataset_version': version,
            'independent_annotation_unchanged': file_digest(annotations) == frozen['annotation_sha256'],
            'initial_receipts_match_snapshot': True,
            'evaluation_receipts_match_snapshot': True,
            'dataset_roundtrip_identical': True, 'fresh_investigation': True,
            'initial_api_calls': execution['api_calls'], 'evaluation_api_calls': replay['api_calls'],
            'initial_actor': execution['actor_directory'], 'evaluation_actor': replay['actor_directory'],
            'skill_up_returncode': returncode,
            'case_results': [{'case_id': c['case_id'], 'status': c['status'], 'grading': c['grading']}
                             for c in native['case_results']],
            'data_origin': '公开教学故障快照；两轮真实 Hermes 调用，不是生产事故'}


def main():
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--probe', action='store_true', help='只检查原生插件、工具与隔离，不调用模型')
    mode.add_argument('--live', action='store_true', help='两轮真实调查并写入指定 Langfuse 数据集')
    parser.add_argument('--task', type=Path, default=HERE / 'fixtures/investigation.json')
    parser.add_argument('--annotations', type=Path, default=HERE / 'fixtures/annotations.json')
    parser.add_argument('--snapshots', type=Path, default=HERE / 'fixtures/snapshots')
    parser.add_argument('--skill', type=Path, default=HERE / 'skills/oncall-investigation/SKILL.md')
    parser.add_argument('--dataset', help='独立的新数据集名称；--live 必须指定')
    parser.add_argument('--resume', action='store_true', help='仅接续采集阶段失败；不重复已完成的调查')
    parser.add_argument('--out', type=Path, default=HERE / 'runtime' / ('pipeline-' + uuid.uuid4().hex[:10]))
    args = parser.parse_args()
    if args.live and (not args.dataset or not all(os.environ.get(k) for k in ('GLM_API_KEY', 'GLM_BASE_URL'))):
        parser.error('--live 需要 --dataset、GLM_API_KEY 和 GLM_BASE_URL')
    output = inside(args.out)
    if args.resume:
        if (not args.live or not (output / 'report.json').is_file()
                or read(output / 'report.json').get('status') != 'pipeline_failed'
                or (output / 'prepared').exists() or (output / 'evaluation').exists()):
            parser.error('--resume 只允许接续尚未制题、未启动评测的采集失败')
        save(output / 'attempts' / (uuid.uuid4().hex + '.json'), read(output / 'report.json'))
    elif output.exists():
        parser.error('输出目录已存在，不能覆盖之前的执行记录')
    lf = None
    try:
        if args.live:
            lf = client()
            if not lf.auth_check():
                raise RuntimeError('Langfuse 项目认证失败；没有调用模型')
            try:
                lf.get_dataset(args.dataset)
            except Exception as exc:
                if getattr(exc, 'status_code', None) != 404:
                    raise
            else:
                raise ValueError('数据集已经存在，请使用新名称，不能混入其他题目')
        report = run(output, args.task.resolve(), args.annotations.resolve(), args.snapshots.resolve(),
                     args.skill.resolve(), args.dataset, args.probe, lf, args.resume)
    except Exception as exc:
        report = {'status': 'pipeline_failed', 'error': type(exc).__name__ + ': ' + str(exc)}
        save(output / 'report.json', report)
        print(json.dumps(report, ensure_ascii=False))
        raise SystemExit(1)
    finally:
        if lf is not None:
            lf.shutdown()
    save(output / 'report.json', report)
    print(output / 'report.json')


if __name__ == '__main__':
    main()

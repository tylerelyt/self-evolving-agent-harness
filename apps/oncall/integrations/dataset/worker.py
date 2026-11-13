"""隔离的 Hermes 进程：仅能读任务、快照、当前技能和运行依赖。"""
import contextlib
import hashlib
import io
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import uuid

from monitor import query, tool_schema


OUTPUT_PROTOCOL = 'oncall-json-v2'
OUTPUT_INSTRUCTION = """本次调查由程序接收结果，不是面向人的聊天报告。
完成工具查询以后，最终回答必须是一个可直接由 JSON 解析器读取的对象。
不要添加开场白、Markdown 代码围栏、表格或对象以外的总结。
对象包含 root_cause（集群/服务或 null）、affected_service（集群/服务）、
excluded_cluster（已排除的集群或 null）、evidence_ids（实际查到的证据编号数组）、
explanation（因果说明字符串）。没有足够依据时保留未知，不编造字段值。
这是所有候选共用的输出接口，Skill 可以改变调查方法，但不能改变这个接口。"""


def request_options():
    return {'temperature': 0, 'response_format': {'type': 'json_object'},
            'extra_body': {'thinking': {'type': 'disabled'}}}


def json_object(text):
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        return False


def completed(result):
    return result.get('completed') is True and not any(
        result.get(k) for k in ('failed', 'error', 'partial', 'interrupted'))


def report_values(text):
    """只用于核对格式修正未改写内容，不把截取的对象当作模型最终回答。"""
    decoder = json.JSONDecoder()
    reports = []
    required = {'root_cause', 'affected_service', 'excluded_cluster', 'evidence_ids', 'explanation'}
    for index, char in enumerate(text):
        if char != '{':
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and required <= value.keys() and value not in reports:
            reports.append(value)
    return reports[0] if len(reports) == 1 else None


def finish_report(result, repair):
    """只反馈 JSON 语法错误一次，不查看评分、标准答案或缺失证据。"""
    first = result.get('final_response') or ''
    if not completed(result) or json_object(first):
        return result, None
    corrected = repair(first)
    final = corrected.get('final_response') or ''
    valid = json_object(final)
    source_values = report_values(first)
    unchanged = source_values is not None and valid and json.loads(final) == source_values
    record = {'initial_response': first, 'corrected_response': final,
              'api_calls': corrected.get('api_calls'), 'completed': completed(corrected),
              'format_valid': valid, 'content_preserved': unchanged,
              'messages': corrected.get('messages', [])}
    if not completed(corrected):
        return corrected, record
    # 修正失败或改变了调查内容，不用新答案替原答案获得更高分。
    return (corrected if unchanged else result), record


def invoke(probe=False):
    work = Path(os.environ['ACTOR_WORK'])
    home = Path(os.environ['HERMES_HOME'])
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('memory:\n  memory_enabled: false\n  user_profile_enabled: false\ncompression:\n  enabled: false\ntools:\n  tool_search:\n    enabled: false\n')
    observed = os.environ.get('LECTURE19_OBSERVE') == '1'
    if observed:
        if importlib.metadata.version('langfuse') != os.environ['LANGFUSE_EXPECTED_VERSION']:
            raise RuntimeError('原生插件的 Langfuse SDK 版本不符')
        from langfuse._client.span import LangfuseChain
        if not callable(getattr(LangfuseChain, 'set_trace_io', None)):
            raise RuntimeError('该 SDK 不支持原生插件使用的 set_trace_io；未调用模型')
        with (home / 'config.yaml').open('a') as config:
            config.write('plugins:\n  enabled:\n    - observability/langfuse\n')
    (home / 'skills').symlink_to(work / 'skills', target_is_directory=True)
    denied = []
    for path in json.loads(os.environ['FORBIDDEN_READS']):
        try:
            Path(path).read_bytes()
        except PermissionError:
            denied.append(path)
        else:
            raise RuntimeError('评分材料可读，停止执行')
    snapshot = json.loads((work / 'snapshot.json').read_text())
    messages = json.loads((work / 'messages.json').read_text())
    import hermes_cli.env_loader
    hermes_cli.env_loader.load_hermes_dotenv = lambda **kwargs: []
    from agent.skill_commands import build_preloaded_skills_prompt
    from tools.registry import registry
    from model_tools import get_tool_definitions
    if observed:
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        manager.discover_and_load()
        loaded_plugins = manager.list_plugins()
        (work / 'home/plugins.json').write_text(json.dumps(loaded_plugins, ensure_ascii=False, indent=2))
        plugin = next((p for p in loaded_plugins if p['key'] == 'observability/langfuse'), {})
        if not plugin.get('enabled') or plugin.get('error') or plugin.get('hooks') != 6:
            raise RuntimeError('Hermes 原生 Langfuse 插件未成功注册六个运行钩子')

    receipts = []

    def monitor(args, **kwargs):
        try:
            receipt = query(snapshot, **args)
        except (TypeError, KeyError, ValueError) as exc:
            receipt = {'error': str(exc), 'query': args}
        receipts.append(receipt)
        return json.dumps(receipt, ensure_ascii=False)

    registry.register(name='monitor_query', toolset='incident_snapshot', schema=tool_schema(snapshot),
                      handler=monitor, check_fn=lambda: True)
    prompt, names, missing = build_preloaded_skills_prompt(['oncall-investigation'])
    if missing or names != ['oncall-investigation']:
        raise RuntimeError('Hermes 未加载指定技能')
    skill_bytes = (work / 'skills/oncall-investigation/SKILL.md').read_bytes()
    if skill_bytes.decode('utf-8').strip() not in prompt:
        raise RuntimeError('Hermes 预载提示与候选文件不同，不能按原文件计算成绩')
    loaded = {'loaded_skill_sha256': hashlib.sha256(skill_bytes).hexdigest(),
              'preloaded_prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
              'output_protocol': OUTPUT_PROTOCOL, 'request_overrides': request_options()}
    (work / 'home/preloaded-skill.txt').write_text(prompt)
    execution_prompt = prompt + '\n\n' + OUTPUT_INSTRUCTION
    (work / 'home/execution-prompt.txt').write_text(execution_prompt)
    submitted_prompt = messages[-1]['content'] + '\n\n' + OUTPUT_INSTRUCTION
    loaded['submitted_prompt'] = submitted_prompt
    definitions = get_tool_definitions(enabled_toolsets=['incident_snapshot'], quiet_mode=True)
    if [d['function']['name'] for d in definitions] != ['monitor_query']:
        raise RuntimeError('工具集合不符合只读约束')
    if probe:
        coverage = snapshot['coverage']
        cluster = next(iter(snapshot['manifest']['clusters']))
        monitor(dict(kind='alerts', cluster=cluster, start=coverage['start'], end=coverage['end']))
        return {**loaded, 'status': 'probe_only', 'final_message': '', 'receipts': receipts,
                'isolation_denied': len(denied), 'skill_loaded': names, 'model_called': False,
                'tool_definitions': definitions}

    if os.environ.get('LECTURE19_ALLOW_MODEL') != '1':
        raise RuntimeError('未显式允许模型请求')
    from run_agent import AIAgent
    session_id, task_id = 'dataset-' + uuid.uuid4().hex, 'investigation-' + uuid.uuid4().hex
    agent = AIAgent(model=os.environ['EVAL_MODEL'], provider='custom', api_mode='chat_completions',
                    api_key=os.environ['GLM_API_KEY'], base_url=os.environ['GLM_BASE_URL'],
                    enabled_toolsets=['incident_snapshot'], max_iterations=int(os.environ.get('EVAL_MAX_ITERATIONS', '12')), max_tokens=4096,
                    request_overrides=request_options(),
                    ephemeral_system_prompt=execution_prompt, quiet_mode=True, skip_context_files=True,
                    skip_memory=True, load_soul_identity=False, save_trajectories=False,
                    session_id=session_id)
    if {t['function']['name'] for t in agent.tools} != {'monitor_query'}:
        raise RuntimeError('Hermes 意外加载了额外工具')
    agent.compression_enabled = False
    result = agent.run_conversation(submitted_prompt, conversation_history=messages[:-1], task_id=task_id)
    investigation = result
    format_task_id = task_id + '-format'

    def repair(first):
        # 独立、无工具的 Hermes 只重交已有结论，不能追加调查或读取答案。
        formatter = AIAgent(model=os.environ['EVAL_MODEL'], provider='custom', api_mode='chat_completions',
            api_key=os.environ['GLM_API_KEY'], base_url=os.environ['GLM_BASE_URL'],
            enabled_toolsets=[], max_iterations=2, max_tokens=4096, request_overrides=request_options(),
            ephemeral_system_prompt=OUTPUT_INSTRUCTION, quiet_mode=True, skip_context_files=True,
            skip_memory=True, load_soul_identity=False, save_trajectories=False, session_id=session_id + '-format')
        if formatter.tools:
            raise RuntimeError('格式修正阶段不应有工具')
        formatter.compression_enabled = False
        return formatter.run_conversation(
            '上一份回答不是纯 JSON。请只重新提交其中已经给出的调查结论，保留原有字段值、证据编号和因果判断。'
            '不得增加新事实，也不要继续调查。只输出 JSON 对象，不加任何其他文字。\n\n原回答：\n' + first,
            task_id=format_task_id)

    result, format_repair = finish_report(result, repair)
    success = completed(result)
    telemetry = None
    if observed:
        from langfuse import Langfuse
        telemetry = {'plugin': 'observability/langfuse', 'session_id': session_id, 'task_id': task_id,
                     'expected_trace_id': Langfuse.create_trace_id(seed=f'{session_id}::{task_id}')}
        if format_repair:
            telemetry['format_trace_id'] = Langfuse.create_trace_id(seed=f'{session_id}-format::{format_task_id}')
    return {**loaded, 'status': 'completed' if success else 'execution_failed', 'telemetry': telemetry,
            'final_message': result.get('final_response') or '', 'receipts': receipts,
            'messages': investigation.get('messages', []), 'model_called': True,
            'investigation_final_message': investigation.get('final_response') or '',
            'investigation_api_calls': investigation.get('api_calls'), 'format_repair': format_repair,
            'isolation_denied': len(denied), 'skill_loaded': names,
            'api_calls': (investigation.get('api_calls') or 0) + ((format_repair.get('api_calls') or 0) if format_repair else 0),
            'completion': {k: result.get(k) for k in ('completed', 'failed', 'partial', 'turn_exit_reason')}}


if __name__ == '__main__':
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            result = invoke('--probe' in sys.argv)
    except Exception as exc:
        result = {'status': 'infrastructure_error', 'final_message': '', 'receipts': [],
                  'error': type(exc).__name__ + ': ' + str(exc)}
    output = Path(os.environ['ACTOR_WORK']) / 'home'
    (output / 'execution.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (output / 'worker.log').write_text(captured.getvalue())
    sys.exit(0 if result['status'] in ('completed', 'probe_only') else 70)

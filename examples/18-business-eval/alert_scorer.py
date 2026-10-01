"""按固定故障事实、过滤条件和真实回执评分；不让执行者读取本文件。"""
import json
import re
import shlex
from incidentctl import query, readonly_command


def extract_answer(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('JSON 含重复键')
            value[key] = item
        return value
    def decode(text):
        value = json.loads(text, object_pairs_hook=unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('非法 JSON 常量')))
        if not isinstance(value, dict):
            raise ValueError('回答必须是 JSON 对象')
        return value
    raw = raw if isinstance(raw, str) else ''
    try:
        return {'answer': decode(raw), 'source': 'json', 'strict_json_format_pass': True, 'outside_text': ''}
    except ValueError:
        pass
    fences = re.findall(r'(?m)^\s*(?:`{3,}|~{3,})[^\n]*$', raw)
    block = re.search(r'(?ms)^[ \t]*```json[ \t]*\n(.*?)^[ \t]*```[ \t]*(?:\n|$)', raw)
    if len(fences) == 2 and block:
        try:
            return {'answer': decode(block.group(1)), 'source': 'single-json-block',
                    'strict_json_format_pass': False,
                    'outside_text': (raw[:block.start()] + raw[block.end():]).strip()}
        except ValueError:
            pass
    return {'answer': None, 'source': 'ambiguous-or-invalid', 'strict_json_format_pass': False,
            'outside_text': raw}


def pairs(messages):
    receipts = {m.get('tool_call_id'): m.get('content') for m in messages if m.get('role') == 'tool'}
    calls = []
    for message in messages:
        for call in message.get('tool_calls') or []:
            function = call.get('function', {})
            args, receipt = function.get('arguments', '{}'), receipts.get(call.get('id'))
            for field in ('args', 'receipt'):
                value = args if field == 'args' else receipt
                if isinstance(value, str):
                    try: value = json.loads(value)
                    except ValueError: pass
                if field == 'args': args = value
                else: receipt = value
            calls.append({'tool_call_id': call.get('id'), 'name': function.get('name'), 'args': args, 'receipt': receipt})
    return calls


def tool_trace(messages, tool_names=None):
    """编号必须唯一、先请求后回执，且每个请求恰有一份非空回执。"""
    pending = set(); seen = set(); errors = []
    allowed = {'terminal', 'skills_list', 'skill_view'}
    names = set()
    for message in messages:
        for call in message.get('tool_calls') or []:
            call_id = call.get('id'); names.add(call.get('function', {}).get('name'))
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                errors.append({'reason': '请求编号缺失或重复', 'tool_call_id': call_id})
            else:
                seen.add(call_id); pending.add(call_id)
        if message.get('role') == 'tool':
            call_id = message.get('tool_call_id')
            content = message.get('content')
            if isinstance(content, str):
                try: content = json.loads(content)
                except ValueError: pass
            if not isinstance(call_id, str) or call_id not in pending or content in (None, ''):
                errors.append({'reason': '回执编号无对应请求、重复或内容为空', 'tool_call_id': call_id})
            else:
                pending.remove(call_id)
    errors += [{'reason': '请求缺少回执', 'tool_call_id': x} for x in sorted(pending)]
    return {'paired': bool(seen) and not errors, 'errors': errors,
            'readonly': names <= allowed and (tool_names is None or bool(tool_names) and set(tool_names) <= allowed)}


def payload(call):
    receipt = call.get('receipt')
    if not isinstance(receipt, dict): return {}
    output = receipt.get('output', receipt.get('stdout', ''))
    if not output: return receipt
    try: return json.loads(output)
    except (TypeError, ValueError): return {}


def grade(item, execution):
    try:
        return _grade(item, execution)
    except Exception as exc:
        return {'status': 'infrastructure_failure', 'score': None, 'reason': '评分器异常：' + type(exc).__name__ + ': ' + str(exc)}


def _grade(item, execution):
    if execution.get('infrastructure_error') or execution.get('interrupted'):
        return {'status': 'infrastructure_failure', 'score': None, 'reason': execution.get('infrastructure_error') or '运行被中断'}
    reason = str(execution.get('turn_exit_reason', ''))
    if reason == 'budget_exhausted' or reason.startswith('max_iterations_reached('):
        return {'status': 'task_failure', 'score': {'content_pass': False, 'overall_pass': False, 'format_pass': None},
                'reason': '任务在统一调用轮数内未完成：' + reason}
    if (execution.get('infrastructure_error') or not execution.get('completed') or not execution.get('final_response')
        or any(execution.get(k) for k in ('failed', 'error', 'partial', 'interrupted'))):
        return {'status': 'infrastructure_failure', 'score': None, 'reason': execution.get('infrastructure_error') or '未取得完整回答'}
    calls = pairs(execution.get('messages', []))
    for call in calls:
        receipt = call['receipt']
        if isinstance(receipt, dict) and receipt.get('error_type') == 'policy_violation':
            return {'status': 'task_failure', 'score': {'content_pass': False, 'overall_pass': False, 'format_pass': None},
                    'reason': '执行者违反已公开的只读命令约束'}
        if isinstance(receipt, dict) and (receipt.get('error') or receipt.get('timed_out') or receipt.get('success') is False or receipt.get('exit_code') not in (None, 0)):
            return {'status': 'infrastructure_failure', 'score': None, 'reason': '工具执行失败，保留空评分'}
    parsed = extract_answer(execution['final_response'])
    answer = parsed['answer']
    if answer is None:
        return {'status': 'needs_review', 'score': {'content_pass': None, 'overall_pass': None, 'format_pass': False},
                'reason': '没有唯一可判定的JSON对象，不选取多个块之一或忽略重复键', 'needs_review': True}
    expected = item['expected']
    fields = {k: type(answer.get(k)) is type(v) and answer.get(k) == v for k, v in expected.items() if k != 'affected_services'}
    affected = answer.get('affected_services')
    fields['affected_services'] = (isinstance(affected, list) and all(isinstance(x, str) for x in affected)
                                   and sorted(affected) == sorted(expected['affected_services']))
    canonical = {r['record_id']: r for kind in ('alerts', 'logs', 'traces') for r in item['snapshot'][kind]}
    records = {}; queries = []; query_matches = []
    for call in calls:
        data = payload(call)
        if call['name'] == 'terminal':
            try:
                words = shlex.split(readonly_command(call['args']))
                matched = data == query(item['snapshot'], *words[1:])
            except (TypeError, ValueError, PermissionError):
                matched = False
            query_matches.append(matched)
            if matched:
                queries.append(data)
                for row in data['records']:
                    records[row['record_id']] = row
    citations = answer.get('citations')
    valid = isinstance(citations, list) and all(isinstance(x, str) for x in citations) and len(set(citations)) == len(citations)
    trace = tool_trace(execution.get('messages', []), execution.get('tool_names', []))
    evidence = {
        '请求回执唯一配对': trace['paired'] and trace['readonly'],
        '请求参数与完整回执一致': bool(query_matches) and all(query_matches),
        '已查询告警日志与链路': {q['action'] for q in queries} == {'alerts', 'logs', 'traces'},
        '主方法实际读取': any(c['name'] == 'skill_view' and isinstance(c['args'], dict) and not c['args'].get('file_path')
                            and isinstance(c['receipt'], dict) and c['receipt'].get('success') for c in calls),
        '查询保留集群过滤': bool(queries) and all(q['cluster_filter'] == expected['cluster'] for q in queries),
        '所需依据均已查询并引用': valid and set(item['required_citations']).issubset(citations)
                                and all(x in records for x in citations),
        '引用归属与内容正确': valid and bool(citations) and all(records.get(x) == canonical.get(x) and x in records
                            and records[x]['cluster'] == expected['cluster'] and records[x]['incident_id'] == item['id'] for x in citations),
        '方法和预置记忆未变': execution.get('skills_unchanged') is True and execution.get('memory_before') == execution.get('memory_after'),
    }
    schema_ok = (set(answer) == set(expected) | {'citations', 'explanation'} and valid
                 and isinstance(answer.get('explanation'), str) and bool(answer['explanation'].strip()))
    score = {'business_fields': fields, 'evidence_checks': evidence,
             'business_pass': all(fields.values()), 'trace_pass': all(evidence.values()),
             'schema_pass': bool(schema_ok), 'format_pass': bool(schema_ok and parsed['strict_json_format_pass'])}
    score['content_pass'] = all(score[k] for k in ('business_pass', 'trace_pass', 'schema_pass'))
    needs_review = bool(parsed['outside_text'])
    score['overall_pass'] = None if needs_review else score['content_pass']
    return {'status': 'needs_review' if needs_review else 'evaluated', 'score': score, 'answer': answer,
            'answer_source': parsed['source'], 'outside_text': parsed['outside_text'], 'needs_review': needs_review,
            'human_review': '中文因果说明是否充分仍由读者核对；程序不把任意自然语言当作已完成语义评审。'}

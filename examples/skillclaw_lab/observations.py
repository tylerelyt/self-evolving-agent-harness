"""核对教学快照、Hermes 消息和原生归档；不生成轨迹或修正上游记录。"""
from __future__ import annotations

from collections import Counter
import hashlib
import json

SCOPE = {'service': 'payment-api', 'region': 'cn-east', 'environment': 'production'}


def snapshot_for(case: str) -> dict:
    if case not in ('pool', 'upstream'):
        raise ValueError('未知快照')
    return {
        'query_service_health': {'healthy': True},
        'query_upstream_latency': {'p95_ms': 1500 if case == 'upstream' else 120},
        'query_connection_pool': {'capacity': 80, 'active': 80, 'waiting_requests': 24},
        'query_connection_leases': {'oldest_lease_seconds': 180, 'release_errors': 6},
    }


def query_snapshot(snapshot: dict, name: str, args: dict) -> dict:
    if args != SCOPE:
        return {'error': '本快照仅包含 payment-api / cn-east / production，请核对查询条件。'}
    return dict(snapshot[name])


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def archive_metadata_conflicts(record: dict) -> list[dict]:
    conflicts = []
    for turn in record.get('turns', []):
        raw = {r.get('tool_call_id'): r for r in turn.get('tool_results_raw', [])}
        for result in turn.get('tool_results', []):
            receipt = raw.get(result.get('tool_call_id'), {})
            try:
                value = json.loads(receipt.get('content'))
            except (ValueError, TypeError):
                continue
            if (isinstance(value, dict) and value.get('success') is True and 'error' not in value
                    and result.get('has_error')):
                conflicts.append({'tool_call_id': result['tool_call_id'], 'tool': receipt.get('name'),
                                  'error_type': result.get('error_type'),
                                  'reason': '实际回执 success=true，但上游摘要标记为错误'})
    return conflicts


def tool_events(messages: list[dict]) -> list[dict]:
    """按模型决策轮次配对调用和回执，不用并发 handler 的完成顺序推断因果。"""
    events = {}
    pending = set()
    for index, message in enumerate(messages):
        if message.get('role') == 'assistant':
            if pending:
                raise ValueError('下一轮模型决策前有工具回执缺失。')
            for call in message.get('tool_calls', []):
                call_id = call.get('id')
                if not call_id or call_id in events:
                    raise ValueError('工具调用 ID 缺失或重复。')
                function = call['function']
                arguments = json.loads(function['arguments'])
                if not isinstance(arguments, dict):
                    raise ValueError('工具参数不是对象。')
                events[call_id] = {'id': call_id, 'tool': function['name'], 'arguments': arguments,
                                   'decision_index': index}
                pending.add(call_id)
        elif message.get('role') == 'tool':
            call_id = message.get('tool_call_id')
            if call_id not in pending:
                raise ValueError('工具回执没有对应调用或重复返回。')
            event = events[call_id]
            if message.get('name', message.get('tool_name')) != event['tool']:
                raise ValueError('工具回执名称与调用不一致。')
            content = message.get('content')
            try:
                result = json.loads(content)
            except (TypeError, ValueError):
                result = content
            event.update(result=result, content=content, result_index=index)
            pending.remove(call_id)
    if pending:
        raise ValueError('会话结束时仍有工具没有回执。')
    return list(events.values())


def verify_task(messages: list[dict], calls: list[dict], case: str, answer: str) -> dict:
    events = tool_events(messages)
    snapshot = snapshot_for(case)
    queries = [event for event in events if event['tool'] in snapshot]
    receipt = lambda c: canonical({key: c[key] for key in ('tool', 'arguments', 'result')})
    receipts_match = Counter(map(receipt, queries)) == Counter(map(receipt, calls))
    exact = bool(queries) and all(c['arguments'] == SCOPE and
                                 canonical(c['result']) == canonical(snapshot[c['tool']])
                                 for c in queries)
    final = messages[-1] if messages else {}
    final_matches = bool(isinstance(answer, str) and answer.strip()
                         and final.get('role') == 'assistant' and not final.get('tool_calls')
                         and final.get('content') == answer and final.get('finish_reason') == 'stop')
    health = [e for e in queries if e['tool'] == 'query_service_health']
    upstream = [e for e in queries if e['tool'] == 'query_upstream_latency']
    pool = [e for e in queries if e['tool'] == 'query_connection_pool']
    ordered_pool = any(h['result_index'] < p['decision_index'] and
                       u['result_index'] < p['decision_index']
                       for h in health for u in upstream for p in pool)
    readonly = all(e['tool'] in {*snapshot, 'skills_list', 'skill_view'} for e in events)
    loaded_hashes = []
    for event in events:
        value = event['result']
        if (event['tool'] == 'skill_view' and event['arguments'].get('name') == 'timeout-triage'
                and isinstance(value, dict) and value.get('success') is True):
            content = value.get('raw_content', value.get('content'))
            if isinstance(content, str):
                loaded_hashes.append(hashlib.sha256(content.encode('utf-8')).hexdigest())
    return {'receipt_messages_match': receipts_match, 'snapshot_receipts_match': exact,
            'final_answer_matches': final_matches, 'readonly_tools_only': readonly,
            'health_and_upstream_receipts': bool(health and upstream),
            'pool_after_observed_checks': ordered_pool, 'loaded_skill_hashes': loaded_hashes,
            'events': events}


def verify_archive(record: dict, messages: list[dict], session_id: str, alias: str) -> dict:
    events = tool_events(messages)
    assistants = [m for m in messages if m.get('role') == 'assistant']
    turns = record.get('turns', [])
    if (record.get('session_id') != session_id or record.get('user_alias') != alias
            or not turns or len(turns) != len(assistants) or record.get('num_turns') != len(turns)):
        raise ValueError('归档来源、会话 ID 或模型轮次数不一致。')
    if (not messages or messages[0].get('role') != 'user'
            or turns[0].get('prompt_text') != messages[0].get('content')):
        raise ValueError('归档中的初始任务与实际用户输入不一致。')
    def call_key(call):
        function = call['function']
        return (call['id'], function['name'], json.loads(function['arguments']))

    fallback_turns = []
    for index, (turn, message) in enumerate(zip(turns, assistants)):
        expected_calls = [call_key(c) for c in message.get('tool_calls', [])]
        actual_calls = [call_key(c) for c in turn.get('tool_calls', [])]
        text_matches = turn.get('response_text', '') == (message.get('content') or '')
        if not text_matches and not message.get('content') and expected_calls:
            # 原生代理在无正文工具轮次，把调用列表的JSON用作response_text。
            # 它不是模型最终回答；只有与同轮完整调用列表一致才接受。
            try:
                text_matches = json.loads(turn.get('response_text', '')) == turn['tool_calls']
            except (TypeError, ValueError):
                text_matches = False
            if text_matches:
                fallback_turns.append(index)
        if actual_calls != expected_calls or not text_matches:
            raise ValueError('归档中的模型调用或回答与 Hermes 消息不一致。')
        ids = {call_id for call_id, _, _ in expected_calls}
        expected = {e['id']: e for e in events if e['id'] in ids}
        raw = turn.get('tool_results_raw', [])
        if len(raw) != len(expected) or {r['tool_call_id'] for r in raw} != ids:
            raise ValueError('原生归档缺少或重复工具回执。')
        for result in raw:
            event = expected[result['tool_call_id']]
            if result.get('name') != event['tool'] or result.get('content') != event['content']:
                raise ValueError('原生归档工具回执与实际返回不一致。')
        metadata = turn.get('tool_results', [])
        if len(metadata) != len(expected) or {r['tool_call_id'] for r in metadata} != ids:
            raise ValueError('原生归档工具摘要缺失或重复。')
    conflicts = archive_metadata_conflicts(record)
    return {'complete': True, 'tool_results': len(events), 'metadata_conflicts': conflicts,
            'response_text_fallback_turns': fallback_turns,
            'usable_for_evolution': not conflicts}

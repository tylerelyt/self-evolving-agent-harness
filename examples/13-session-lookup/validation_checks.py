"""只根据原始消息及同编号回执检查，不把助手自述或操作受理当成完成。"""
import hashlib
import json
from common import record, completed


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def pair_messages(messages):
    """重复编号或缺少回执时保留未知；不能按消息相邻关系凑成一对。"""
    requests, receipts = {}, {}
    for message in messages:
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls') or []:
                requests.setdefault(call.get('id'), []).append((message, call))
        if message.get('role') == 'tool':
            receipts.setdefault(message.get('tool_call_id'), []).append(message)
    pairs = []
    for call_id, calls in requests.items():
        answers = receipts.get(call_id, [])
        matched = bool(call_id) and len(calls) == len(answers) == 1
        for message, call in calls:
            function = call.get('function') or {}
            answer = answers[0] if matched else {}
            pairs.append({'tool_call_id': call_id, 'name': function.get('name'),
                          'args': decode(function.get('arguments', '{}')),
                          'request_message_id': message.get('id'),
                          'receipt_message_id': answer.get('id'),
                          'receipt': decode(answer.get('content')),
                          'pairing': 'matched' if matched else 'unknown'})
    return pairs


def receipt_payload(pair):
    receipt = pair.get('receipt')
    if not isinstance(receipt, dict):
        return {}
    payload = decode(receipt.get('output', receipt.get('stdout')))
    return payload if isinstance(payload, dict) else receipt


def execution_state(pair):
    if pair.get('pairing') != 'matched':
        return 'unknown'
    outer = pair.get('receipt')
    if isinstance(outer, dict) and (outer.get('error') or outer.get('exit_code', 0) != 0):
        return 'failed'
    payload = receipt_payload(pair)
    if payload.get('success') is False or payload.get('status') in ('failed', 'error'):
        return 'failed'
    if pair.get('name') == 'memory' and payload.get('staged') is True:
        return 'accepted'
    if payload.get('status') in ('denied', 'accepted', 'completed'):
        return payload['status']
    if pair.get('name') == 'memory' and payload.get('success') is True and payload.get('done') is True:
        return 'completed'
    return 'unknown'


def memory_observations(pairs):
    """报告源操作的真实状态；失败或暂存不算写入完成。"""
    records = []
    for pair in pairs:
        if pair['name'] != 'memory':
            continue
        args = pair['args'] if isinstance(pair['args'], dict) else {}
        operations = args.get('operations') or [args]
        write = (args.get('target', 'memory') == 'memory'
                 and any(isinstance(op, dict) and op.get('action') in ('add', 'replace') for op in operations))
        state = execution_state(pair)
        records.append({**pair, 'execution_state': state,
                        'write_completed': bool(write and state == 'completed')})
    return records


def successful_search(pair):
    return (pair['pairing'] == 'matched' and isinstance(pair['args'], dict)
            and isinstance(pair['receipt'], dict) and pair['receipt'].get('success') is True)


def visible_pair_ids(searches, source):
    """核查模型实际读到了同一对原文，不能只展开标题就算溯源。"""
    messages = {}
    for search in searches:
        if not successful_search(search) or search['args'].get('session_id') != source['session_id']:
            continue
        for message in search['receipt'].get('messages', []):
            if isinstance(message, dict) and message.get('id') is not None:
                messages[message['id']] = message
    seen = pair_messages(list(messages.values()))
    return {pair['tool_call_id'] for pair in source['pairs']
            if pair['pairing'] == 'matched' and pair in seen}


def digest(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def scroll_messages(db, session_id, anchor, window=2):
    """小窗口向前、向后展开；不调用模型，不将返回消息二次摘要。"""
    from tools.session_search_tool import session_search
    messages, windows = {}, []
    origin = anchor
    for direction in ('before', 'after'):
        anchor = origin
        for _ in range(200):
            view = json.loads(session_search(db=db, session_id=session_id,
                                            around_message_id=anchor, window=window))
            require(view.get('success') and view.get('messages'), '窗口没有返回原始消息，结果未知')
            raw = view['messages']
            windows.append({'direction': direction, 'anchor': anchor, 'response': view})
            messages.update({m['id']: m for m in raw})
            if not view.get('messages_' + direction):
                break
            next_anchor = raw[0 if direction == 'before' else -1]['id']
            require(next_anchor != anchor, '窗口没有前进，不能宣称已经读全')
            anchor = next_anchor
        else:
            raise RuntimeError('超过窗口数量上限；未查到的部分保持未知')
    return [messages[k] for k in sorted(messages)], windows


def inspect_session(db, query, session_id):
    from tools.session_search_tool import session_search
    # 不传 role_filter：按原生缺省规则发现 user/assistant，而非假设可搜到每条 tool。
    discovery = json.loads(session_search(db=db, query=query, limit=20))
    require(discovery.get('success'), '原生搜索失败')
    hit = next((h for h in discovery.get('results', []) if h['session_id'] == session_id), None)
    require(hit is not None, '未发现目标会话；需检查关键词和覆盖范围，不能推断事情没有发生')
    messages, windows = scroll_messages(db, session_id, hit['match_message_id'])
    return {'discovery': discovery, 'session_id': session_id, 'windows': windows,
            'messages': messages, 'pairs': pair_messages(messages)}


def verify_live(db_path, tasks, directory, before_audit):
    from hermes_state import SessionDB
    db = SessionDB(db_path)
    try:
        memory = inspect_session(db, 'lesson13-memory', tasks['memory']['session'])
        incident = inspect_session(db, 'rollback', tasks['incident']['session'])
        audit_messages = db.get_messages(tasks['audit']['session'], include_inactive=True)
        after = {sid: digest(db.get_messages(sid, include_inactive=True)) for sid in before_audit}
    finally:
        db.close()
    reopened = SessionDB(db_path)
    try:
        persisted = all(digest(reopened.get_messages(sid, include_inactive=True)) == value
                        for sid, value in after.items())
    finally:
        reopened.close()
    memory_calls = [p for p in memory['pairs'] if p['name'] == 'memory']
    memory_file = db_path.parent / 'memories/MEMORY.md'
    pairs = incident['pairs']
    command = lambda p: p['args'].get('command', '') if isinstance(p['args'], dict) else ''
    rollbacks = [p for p in pairs if 'rollback' in command(p)]
    pauses = [p for p in pairs if 'pause repeat-pay' in command(p) and execution_state(p) == 'completed']
    pause_end = max((p['receipt_message_id'] for p in pauses), default=-1)
    later = [p for p in pairs if (p['request_message_id'] or -1) > pause_end]
    denied_rollbacks = [p for p in rollbacks if execution_state(p) == 'denied'
                        and receipt_payload(p).get('task_created') is False]
    describes = [p for p in later if p['pairing'] == 'matched' and execution_state(p) != 'failed'
                 and receipt_payload(p).get('batch_active') is False
                 and receipt_payload(p).get('version') == 'sample-v2' and 'describe' in command(p)]
    logs = [p for p in later if p['pairing'] == 'matched' and execution_state(p) != 'failed'
            and 'logs' in command(p) and len(receipt_payload(p).get('rows', [])) >= 10
            and all(row.get('status') == 200 for row in receipt_payload(p)['rows'][-10:])]
    audit_calls = pair_messages(audit_messages)
    searches = [p for p in audit_calls if p['name'] == 'session_search']
    memory_seen = visible_pair_ids(searches, memory)
    incident_seen = visible_pair_ids(searches, incident)
    memory_states = memory_observations(memory_calls)
    memory_nonempty = memory_file.exists() and bool(memory_file.read_text().strip())
    checks = {
        '事故与核查前台正常完成': all(tasks[key]['status'] == 'completed' and completed(tasks[key]['result']) for key in ('incident', 'audit')),
        '原生Memory请求与同编号回执可核对': any(p['pairing'] == 'matched' for p in memory_calls),
        '核查实际读到Memory请求和原回执': bool(memory_seen),
        '回滚被拒绝而不是已经完成': bool(denied_rollbacks),
        '暂停完成后再次查询业务部署': bool(pauses and describes),
        '暂停完成后再次查询业务日志': bool(pauses and logs),
        '新会话实际发现旧消息': all(any(successful_search(p) and p['args'].get('query')
                                      and any(h.get('session_id') == tasks[key]['session'] for h in p['receipt'].get('results', []))
                                      for p in searches) for key in ('memory', 'incident')),
        '新会话按窗口展开事故与memory记录': all(any(successful_search(p) and p['args'].get('session_id') == tasks[key]['session']
                                                 and p['args'].get('around_message_id') and p['args'].get('window')
                                                 and p['receipt'].get('messages') for p in searches) for key in ('memory', 'incident')),
        '核查实际读到回滚及暂停的原请求回执': (any(p['tool_call_id'] in incident_seen for p in denied_rollbacks)
                                              and any(p['tool_call_id'] in incident_seen for p in pauses)),
        '核查实际读到后续业务查询回执': all(any(p['tool_call_id'] in incident_seen for p in verified)
                                             for verified in (describes, logs)),
        '回查不执行业务或记忆写入': bool(audit_calls) and all(p['name'] == 'session_search' for p in audit_calls),
        '回查前后旧消息不变且重开可查': before_audit == after and persisted,
    }
    result = {'path': 'native-live', 'checks': checks, 'passed': all(checks.values()),
              'report_review_required': True, 'memory': memory, 'incident': incident,
              'audit': {'messages': audit_messages, 'pairs': audit_calls},
              'source_memory': {'task_status': tasks['memory']['status'], 'operations': memory_states,
                                'write_completed': any(p['write_completed'] for p in memory_states),
                                'file_nonempty': memory_nonempty, 'file_presence_is_execution_proof': False},
              'unknown_boundary': '未找到记录不等于未发生；受理不等于完成，操作完成不等于业务恢复或恢复原因。'}
    record(directory / 'history-check.json', result)
    return result

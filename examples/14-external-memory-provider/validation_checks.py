"""核查工具回执、共享记录及同步状态；前台回答不能代替写入成功。"""
import hashlib
import json
from common import record, tool_calls, completed


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def receipt_payload(call):
    try:
        return json.loads((call.get('receipt') or {}).get('output', ''))
    except (TypeError, ValueError):
        return {}


def restore_db(path, rows):
    from hermes_state import SessionDB
    import inspect
    import sqlite3
    db = SessionDB(path)
    # 保留本版本原生消息接口接收的全部字段，包括API文本与供应商元数据。
    fields = set(inspect.signature(db.append_message).parameters) - {'session_id'}
    for session in rows:
        db.create_session(session['id'], source='cli')
        db.set_session_title(session['id'], session['title'])
        for m in session['messages']:
            inserted = db.append_message(session['id'], **{k: v for k, v in m.items() if k in fields})
            if inserted != m['id']:
                db._conn.execute('UPDATE messages SET id=? WHERE id=?', (m['id'], inserted))
                db._conn.commit()
    db.close()
    with sqlite3.connect(path) as db:
        db.executemany('UPDATE sessions SET started_at=? WHERE id=?', [(s['started_at'], s['id']) for s in rows])


def verify_history(path, rows, directory):
    from hermes_state import SessionDB
    digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    db = SessionDB(path)
    try:
        before = {s['id']: digest(db.get_messages(s['id'], include_inactive=True)) for s in rows}
        matched = all(before[s['id']] == digest(s['messages']) for s in rows)
        paired = {s['id']: tool_calls(db.get_messages(s['id'], include_inactive=True)) for s in rows}
    finally:
        db.close()
    db = SessionDB(path)
    try:
        reopened = all(before[s['id']] == digest(db.get_messages(s['id'], include_inactive=True)) for s in rows)
    finally:
        db.close()
    result = {'source_messages_equal': matched, 'reopen_verified': reopened, 'sha256': before, 'paired_calls': paired}
    record(directory / 'history-check.json', result)
    return result


def evaluate(tasks, reviews, rows, business, provider, failure, recovery, history, snapshots, live=True):
    events, records = provider['events'], provider['records']
    cases = [r for r in records if r['kind'] == 'case']
    source_case = lambda task: [r for r in cases if r['source']['session'] == task['session']]
    a, b, read, write = tasks
    a_cases, b_cases = source_case(a), source_case(b)
    recall_b = [e for e in events if e['action'] == 'prefetch' and e['session'] == b['session'] and e.get('status') == 'completed']
    hits_b = [r for e in recall_b for r in e['receipt']['records']]
    feedback = [r for e in events if e['action'] == 'prefetch' and e['session'] == a['session'] for r in e.get('receipt', {}).get('records', [])]
    b_receipts = [receipt_payload(c) for c in b['calls']]
    b_actions = [r for r in b_receipts if r.get('action')]
    failures = [p for p in failure['pending'] if p['session'] == write['session']]
    recovered = [p for p in provider['pending'] if p['session'] == write['session']]
    failed_ids = {p['id'] for p in failures}
    before_ids = {r['id'] for r in failure['records']}
    backend_ids = [r['id'] for r in records]
    timeouts = [e for e in events if e['session'] == read['session'] and e.get('status') == 'timed_out']
    denied = [e for e in events if e['action'] == 'prefetch' and e.get('status') == 'denied']
    raw_records = {r['id']: r for r in records if r['kind'] == 'raw'}
    raw_pairs = {sid: tool_calls(s['messages']) for s in rows for sid in [s['id']]}
    provenance = bool(cases)
    for case in cases:
        sid = case['source']['session']
        pairs = {c['tool_call_id']: receipt_payload(c) for c in raw_pairs.get(sid, [])}
        provenance = provenance and case['raw_id'] in raw_records and bool(case['evidence']) and all(
            pairs.get(e['tool_call_id']) == e['receipt'] for e in case['evidence'])
    native_memories = [r.get('memory_after', '') for rs in reviews for r in rs]
    builtin_success = any(c['name'] == 'memory' and (c.get('receipt') or {}).get('success')
                          for rs in reviews for r in rs for c in r.get('calls', []))
    changes = [e for e in events if e['action'] in ('sync_turn', 'retry')]
    audit = [json.loads(line) for line in (business / 'audit.jsonl').read_text().splitlines()]
    checks = {
        '原生配置装载并按会话初始化': all(s['provider_names'] == ['oncall-memory'] for s in snapshots) and all(any(e['action'] == 'initialize' and e['session'] == t['session'] for e in events) for t in tasks),
        'A先召回共享手册再以回执形成条件案例': any(e['session'] == a['session'] and any(r['kind'] == 'manual' for r in e.get('receipt', {}).get('records', [])) for e in events) and bool(a_cases) and a_cases[0]['cause'] == '入库任务暂停' and '报表未更新' in a_cases[0]['condition'],
        'B每轮召回A案例并保留来源范围时间': bool(a_cases) and any(r['id'] == a_cases[0]['id'] and r['source']['instance'] == 'A' and r['scope'] == 'report-prod-v1' and r['source']['time'] for r in hits_b),
        'B实际查入库正常并继续查上游': any(r.get('action') == 'ingestion' and r.get('paused') is False for r in b_actions) and any(r.get('action') == 'upstream' and r.get('field') != r.get('expected') for r in b_actions),
        'B未照搬A处置而写回差异': not any(r.get('action') == 'resume' for r in b_actions) and bool(b_cases) and b_cases[0]['cause'] == '上游字段变化' and any(r.get('action') == 'map-field' and r.get('status') == 'completed' for r in b_actions),
        'A继续召回B反馈': bool(b_cases) and any(r['id'] == b_cases[0]['id'] for r in feedback),
        '读超时本轮无召回前台继续调查': bool(timeouts) and all(e['receipt']['records'] == [] for e in timeouts) and completed(read['result']) and any(receipt_payload(c).get('action') == 'ingestion' for c in read['calls']) and not any(e['session'] == read['session'] and e.get('status') == 'completed' for e in events if e['action'] == 'prefetch'),
        '写失败前台正常但经历未提交并保留原因': completed(write['result']) and bool(failures) and all(p['status'] == 'failed' and p['error'] and p['id'] not in before_ids and 'case-' + p['id'] not in before_ids for p in failures) and write.get('sync_worker_drained') is True,
        '重启后仍从持久队列补传': recovery['pid_before'] != recovery['pid_after'] and bool(failed_ids) and failed_ids == {p['id'] for p in recovered} and all(p['status'] == 'case_ready' and p['body']['case']['id'] in backend_ids for p in recovered) and any(e['action'] == 'retry' and e['record_id'] in failed_ids and e['receipt']['case_ready'] for e in events),
        '稳定编号重复补传不重复写入': recovery['count_after_recovery'] == recovery['count_after_repeat'] and len(backend_ids) == len(set(backend_ids)) and all(p['attempts'] >= 4 for p in recovered),
        '越业务范围拒绝且旧版本不混入': bool(denied) and all(e['receipt']['records'] == [] for e in denied) and all(r['scope'] == 'report-prod-v1' and r.get('version', 'v1') == 'v1' for e in events for r in e.get('receipt', {}).get('records', [])),
        '内置Memory与外部后端并非全量双向同步': builtin_success and any(native_memories) and all('入库任务暂停' not in m and '上游字段变化' not in m for m in native_memories) and all('kind' in r for r in records) and not any(r['kind'] == 'builtin' for r in records),
        '每次写召回有前后状态及回执': bool(changes) and all(all(k in e for k in ('before', 'after', 'receipt')) for e in events if e['action'] in ('prefetch', 'sync_turn', 'retry')),
        '案例依据配对原始回执且Session重开可查': provenance and history['source_messages_equal'] and history['reopen_verified'] and all(completed(t['result']) for t in tasks) and all(r['status'] == 'completed' for rs in reviews for r in rs) and all(json.loads((business / 'state.json').read_text())[x]['fresh'] for x in ('A', 'B')) and bool(audit),
    }
    for label, ok in checks.items():
        print(('通过' if ok else '未通过') + '｜' + label)
    return {'checks': checks, 'passed': all(checks.values()), 'path': 'native-live' if live else 'recorded-replay'}

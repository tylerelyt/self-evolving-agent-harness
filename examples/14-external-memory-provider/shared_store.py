"""共享资料库与持久待传队列；这里只处理程序可核对的提交状态。"""
import json
import sqlite3
from common import runtime, now

SCOPE = 'report-prod-v1'


def business():
    from pathlib import Path
    return Path(json.loads((runtime() / 'business.json').read_text())['directory'])


def backend():
    return business() / 'shared.sqlite3'


def faults():
    path = runtime() / 'faults.json'
    return json.loads(path.read_text()) if path.exists() else {}


def schema():
    local_schema()
    backend_schema()


def backend_schema():
    with sqlite3.connect(backend()) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, scope TEXT, kind TEXT, body TEXT)')


def local_schema():
    """本地入队不能以远端可连接为前提。"""
    path = runtime() / 'hermes/providers/outbox.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, session TEXT, body TEXT, status TEXT, error TEXT, attempts INTEGER)')


def rows(scope=None):
    with sqlite3.connect(backend()) as db:
        result = db.execute('SELECT body FROM records ORDER BY id' if scope is None else
                            'SELECT body FROM records WHERE scope=? ORDER BY id', () if scope is None else (scope,)).fetchall()
    return [json.loads(r[0]) for r in result]


def pending():
    local_schema()
    with sqlite3.connect(runtime() / 'hermes/providers/outbox.sqlite3') as db:
        db.row_factory = sqlite3.Row
        result = [dict(r) for r in db.execute('SELECT * FROM pending ORDER BY id')]
    for r in result:
        r['body'] = json.loads(r['body'])
    return result


def snapshot():
    queued = pending()
    try:
        return {'records': rows(), 'pending': queued}
    except (OSError, sqlite3.Error) as exc:
        return {'records': None, 'pending': queued, 'backend_error': type(exc).__name__}


def enqueue(item):
    local_schema()
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(runtime() / 'hermes/providers/outbox.sqlite3') as db:
        db.execute('INSERT INTO pending VALUES (?,?,?,\'pending\',\'\',0) ON CONFLICT(id) DO NOTHING',
                   (item['id'], item['source']['session'], encoded))
        old = db.execute('SELECT body FROM pending WHERE id=?', (item['id'],)).fetchone()[0]
        if old != encoded:
            raise ValueError('同一记录编号对应不同内容，拒绝覆盖')


def transmit(item, force=False):
    """后端按稳定编号幂等；提交后再核对原始经历与案例的可查状态。"""
    path = runtime() / 'hermes/providers/outbox.sqlite3'
    error = ''
    try:
        if faults().get('write_fail'):
            raise OSError('构造共享后端写入失败')
        backend_schema()
        case = item.get('case')
        records = [item['raw']] + ([case] if case else [])
        with sqlite3.connect(backend(), timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            for rec in records:
                body = json.dumps(rec, ensure_ascii=False, sort_keys=True)
                db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING',
                           (rec['id'], rec['scope'], rec['kind'], body))
                if db.execute('SELECT body FROM records WHERE id=?', (rec['id'],)).fetchone()[0] != body:
                    raise ValueError('后端编号冲突，未覆盖已有经历')
        found = {r['id'] for r in rows(item['scope'])}
        if not all(r['id'] in found for r in records):
            raise RuntimeError('提交后的回读缺少经历或案例')
        status = 'case_ready' if case else 'raw_saved'
    except Exception as exc:
        status, error = 'failed', f'{type(exc).__name__}: {exc}'
    with sqlite3.connect(path) as db:
        db.execute('UPDATE pending SET status=?,error=?,attempts=attempts+1 WHERE id=?',
                   (status, error, item['id']))
    return {'id': item['id'], 'status': status, 'error': error, 'at': now(),
            'raw_saved': status in ('raw_saved', 'case_ready'), 'case_ready': status == 'case_ready',
            'forced_retry': force}

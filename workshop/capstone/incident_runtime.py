"""结课演练的在线事件与处置状态机。存储和状态转换真实，业务观测与处置使用本机夹具。"""
from contextlib import closing
import json
import sqlite3


class IncidentRuntime:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL,
                    skill TEXT, evidence TEXT, healthy_windows INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    key TEXT PRIMARY KEY, incident TEXT, kind TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, incident TEXT NOT NULL, scope TEXT NOT NULL,
                    action TEXT NOT NULL, approved_by TEXT, receipt TEXT
                );
            ''')

    def transaction(self, callback):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            return callback(db)

    @staticmethod
    def event(db, key, incident, kind, body):
        db.execute('INSERT INTO events VALUES (?,?,?,?)',
                   (key, incident, kind, json.dumps(body, ensure_ascii=False, sort_keys=True)))

    @staticmethod
    def change(db, incident, expected, target):
        if db.execute('UPDATE incidents SET state=? WHERE id=? AND state=?',
                      (target, incident, expected)).rowcount != 1:
            raise ValueError(f'事件 {incident} 当前不可执行 {expected} → {target}')

    def observe(self, key, incident, scope, total, errors, *, source='patrol', quality=None, evidence=None):
        if total < 0 or not 0 <= errors <= total:
            raise ValueError('观测计数无效')
        if quality is None:
            quality = 'insufficient' if total < 20 else 'unhealthy' if errors / total >= 0.10 else 'healthy'
        if quality not in ('healthy', 'insufficient', 'unhealthy'):
            raise ValueError('观测质量无效')
        body = {'scope': scope, 'total': total, 'errors': errors, 'quality': quality, 'source': source}
        if evidence is not None:
            if not isinstance(evidence, dict):
                raise ValueError('观测证据必须为对象')
            body['evidence'] = evidence
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)

        def record(db):
            prior = db.execute('SELECT incident,kind,body FROM events WHERE key=?', (key,)).fetchone()
            if prior:
                if (prior['incident'], prior['kind'], prior['body']) != (incident, 'observation', encoded):
                    raise ValueError('事件键已对应另一份观测')
                return {'deduplicated': True, 'quality': quality}
            row = db.execute('SELECT scope,state,healthy_windows FROM incidents WHERE id=?', (incident,)).fetchone()
            if row and row['scope'] != scope:
                raise ValueError('观测范围与事件不一致')
            self.event(db, key, incident, 'observation', body)
            if quality == 'unhealthy' and not row:
                db.execute('INSERT INTO incidents(id,scope,state) VALUES (?,?,?)', (incident, scope, 'OPEN'))
            if row and row['state'] in ('VERIFYING', 'VERIFIED'):
                count = row['healthy_windows'] + 1 if quality == 'healthy' else 0
                db.execute('UPDATE incidents SET healthy_windows=?,state=? WHERE id=?',
                           (count, 'VERIFIED' if count >= 2 else 'VERIFYING', incident))
            return {'deduplicated': False, 'quality': quality}

        return self.transaction(record)

    def start(self, incident, skill):
        def record(db):
            self.change(db, incident, 'OPEN', 'INVESTIGATING')
            db.execute('UPDATE incidents SET skill=? WHERE id=?', (skill, incident))
            self.event(db, incident + '-start', incident, 'investigation_started', {'skill_sha256': skill})
        self.transaction(record)

    def request_action(self, incident, operation, evidence, *, action='restore_fixture_dependency'):
        if not isinstance(action, str) or not action.strip():
            raise ValueError('处置请求须指定动作')

        def record(db):
            self.change(db, incident, 'INVESTIGATING', 'AWAITING_APPROVAL')
            scope = db.execute('SELECT scope FROM incidents WHERE id=?', (incident,)).fetchone()['scope']
            db.execute('UPDATE incidents SET evidence=? WHERE id=?', (evidence, incident))
            db.execute('INSERT INTO operations(id,incident,scope,action) VALUES (?,?,?,?)',
                       (operation, incident, scope, action))
            self.event(db, incident + '-request', incident, 'action_requested',
                       {'operation': operation, 'scope': scope, 'action': action, 'evidence': evidence})
        self.transaction(record)

    def approve(self, incident, operation, scope, actor):
        if not actor:
            raise ValueError('审批必须记录演练值班员身份')

        def record(db):
            approved = db.execute('''UPDATE operations SET approved_by=?
                WHERE id=? AND incident=? AND scope=? AND approved_by IS NULL''',
                                  (actor, operation, incident, scope))
            if approved.rowcount != 1:
                raise ValueError('审批与请求的动作、事件或范围不匹配')
            self.change(db, incident, 'AWAITING_APPROVAL', 'APPROVED')
            self.event(db, incident + '-approved', incident, 'action_approved',
                       {'operation': operation, 'scope': scope, 'actor': actor})
        self.transaction(record)

    def execute(self, incident, operation, scope, *, apply=None):
        """Record an approved action. A local callback must be safe to retry if commit fails."""
        def record(db):
            row = db.execute('SELECT * FROM operations WHERE id=? AND incident=? AND scope=?',
                             (operation, incident, scope)).fetchone()
            if not row or not row['approved_by']:
                raise ValueError('找不到相同事件和范围的已批准动作')
            if row['receipt']:
                return json.loads(row['receipt'])
            if apply is None and row['action'] != 'restore_fixture_dependency':
                raise ValueError('该处置动作需要演练执行器')
            self.change(db, incident, 'APPROVED', 'VERIFYING')
            receipt = {'operation': operation, 'scope': scope, 'action': row['action'],
                       'adapter': 'local_fixture', 'status': 'applied'}
            if apply is not None:
                effect = apply()
                if not isinstance(effect, dict):
                    raise ValueError('演练执行器须返回处置回执对象')
                receipt['effect'] = effect
            db.execute('UPDATE operations SET receipt=? WHERE id=?', (json.dumps(receipt), operation))
            self.event(db, incident + '-execute', incident, 'action_executed', receipt)
            return receipt
        return self.transaction(record)

    def handoff(self, incident, owner, pending):
        def record(db):
            row = db.execute('SELECT state,evidence,healthy_windows FROM incidents WHERE id=?', (incident,)).fetchone()
            self.event(db, incident + '-handoff', incident, 'handoff',
                       {'owner': owner, 'pending': pending, **dict(row)})
        self.transaction(record)

    def close(self, incident, actor):
        def record(db):
            self.change(db, incident, 'VERIFIED', 'CLOSED')
            self.event(db, incident + '-closed', incident, 'closed', {'actor': actor})
        self.transaction(record)

    def report(self):
        def read(db):
            return {'incidents': [dict(r) for r in db.execute('SELECT * FROM incidents')],
                    'events': [{**dict(r), 'body': json.loads(r['body'])}
                               for r in db.execute('SELECT * FROM events ORDER BY rowid')]}
        return self.transaction(read)

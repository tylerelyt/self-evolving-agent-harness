"""Durable single-host task and learning-job ownership.

The SQLite lease fences state writes, not already-dispatched external effects.
Adapters still need an idempotent action journal/reconciliation at their tool
boundary. Wall-clock leases require a trustworthy host clock; this is not a
multi-host consensus service.
"""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


class LeaseLost(RuntimeError):
    """The writer no longer owns the task's current execution epoch."""


@dataclass(frozen=True)
class Lease:
    task_id: str
    owner: str
    epoch: int
    expires_at: float


class ControlStore:
    """A durable queue shared by execution workers and background learning.

    ``enqueue`` and ``event`` persist immutable input. ``claim`` returns a Lease
    plus the current task, including any prior checkpoint. Every running write
    requires an unexpired matching lease. Crashed jobs become claimable after
    expiry; ``fail`` schedules bounded retries without discarding evidence.
    """

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with closing(self._connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported control schema")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, queue TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', owner TEXT, epoch INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL, available_at REAL NOT NULL,
                    checkpoint TEXT, session_id TEXT, item_id INTEGER,
                    result TEXT, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(scope,queue,idempotency_key));
                CREATE INDEX IF NOT EXISTS ready_tasks ON tasks(queue,status,available_at,lease_until);
                CREATE TABLE IF NOT EXISTS events (
                    scope TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL,
                    payload TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
                    created_at REAL NOT NULL, PRIMARY KEY(scope,source,event_id));
                CREATE TABLE IF NOT EXISTS transitions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                    kind TEXT NOT NULL, epoch INTEGER NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL);
                PRAGMA user_version=1;
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        value = dict(row)
        for name in ("payload", "checkpoint", "result"):
            if value[name] is not None:
                value[name] = json.loads(value[name])
        return value

    def _record(self, db, task_id, kind, epoch, body):
        db.execute("INSERT INTO transitions(task_id,kind,epoch,body,created_at) VALUES(?,?,?,?,?)",
                   (task_id, kind, epoch, _json(body), self.clock()))

    def _enqueue(self, db, scope, queue, key, payload, max_attempts):
        for item, name in ((scope,"scope"),(queue,"queue"),(key,"idempotency_key")):
            _text(item, name)
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        body = _json(payload)
        old = db.execute("SELECT * FROM tasks WHERE scope=? AND queue=? AND idempotency_key=?",
                         (scope, queue, key)).fetchone()
        if old:
            if old["payload"] != body or old["max_attempts"] != max_attempts:
                raise ValueError("Idempotency key is already bound to different task input")
            return self._decode(old)
        identifier, now = uuid4().hex, self.clock()
        db.execute('''INSERT INTO tasks(id,scope,queue,idempotency_key,payload,max_attempts,
                     available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                   (identifier, scope, queue, key, body, max_attempts, now, now, now))
        self._record(db, identifier, "enqueued", 0, {})
        return self._decode(db.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone())

    def enqueue(self, *, scope, key, payload, queue="execution", max_attempts=3):
        with self._transaction() as db:
            return self._enqueue(db, scope, queue, key, payload, max_attempts)

    def event(self, *, scope, source, event_id, payload, queue="execution", max_attempts=3):
        """Authorize before calling: event ingestion does not establish identity."""
        _text(source, "source")
        _text(event_id, "event_id")
        # Hash avoids delimiter ambiguity and does not expose event text in a key.
        key = "event:" + sha256(_json([source, event_id]).encode()).hexdigest()
        with self._transaction() as db:
            task = self._enqueue(db, scope, queue, key, payload, max_attempts)
            old = db.execute("SELECT * FROM events WHERE scope=? AND source=? AND event_id=?",
                             (scope, source, event_id)).fetchone()
            if old and (old["task_id"] != task["id"] or old["payload"] != _json(payload)):
                raise ValueError("Event identity is already bound to different task input")
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                       (scope, source, event_id, _json(payload), task["id"], self.clock()))
            return task

    def claim(self, owner, *, queue="execution", scope=None, task_id=None, lease_seconds=60):
        _text(owner, "owner")
        _positive(lease_seconds, "lease_seconds")
        with self._transaction() as db:
            now = self.clock()
            # Crashes also consume attempts; exhausted stale jobs stay visible.
            exhausted = db.execute('''SELECT * FROM tasks WHERE queue=? AND status='running'
                AND lease_until<=? AND attempts>=max_attempts''', (queue, now)).fetchall()
            for row in exhausted:
                db.execute("UPDATE tasks SET status='failed',updated_at=? WHERE id=?", (now, row["id"]))
                self._record(db,row["id"],"exhausted",row["epoch"],{"reason":"lease_expired"})
            query = '''SELECT * FROM tasks WHERE queue=? AND available_at<=? AND attempts<max_attempts
                       AND (status='pending' OR (status='running' AND lease_until<=?))'''
            params = [queue, now, now]
            if scope is not None:
                query += " AND scope=?"
                params.append(scope)
            if task_id is not None:
                query += " AND id=?"
                params.append(task_id)
            row = db.execute(query + " ORDER BY created_at,id LIMIT 1", params).fetchone()
            if row is None:
                return None
            epoch, until = row["epoch"] + 1, now + lease_seconds
            db.execute('''UPDATE tasks SET status='running',owner=?,epoch=?,lease_until=?,
                attempts=attempts+1,updated_at=? WHERE id=?''', (owner,epoch,until,now,row["id"]))
            self._record(db,row["id"],"claimed",epoch,{"owner":owner,"lease_until":until})
            current = self._decode(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())
            return Lease(row["id"], owner, epoch, until), current

    def _owned(self, db, lease):
        if not isinstance(lease, Lease):
            raise ValueError("A Lease is required")
        row = db.execute("SELECT * FROM tasks WHERE id=?", (lease.task_id,)).fetchone()
        if (row is None or row["owner"] != lease.owner or row["epoch"] != lease.epoch
                or row["status"] != "running" or row["lease_until"] <= self.clock()):
            raise LeaseLost("Task ownership expired or belongs to a different epoch")
        return row

    def heartbeat(self, lease, *, lease_seconds=60):
        _positive(lease_seconds, "lease_seconds")
        with self._transaction() as db:
            self._owned(db, lease)
            until = self.clock() + lease_seconds
            db.execute("UPDATE tasks SET lease_until=?,updated_at=? WHERE id=?", (until,self.clock(),lease.task_id))
            return Lease(lease.task_id,lease.owner,lease.epoch,until)

    def checkpoint(self, lease, value, *, session_id=None, item_id=None):
        body = _json(value)
        if session_id is not None:
            _text(session_id,"session_id")
        if item_id is not None and (type(item_id) is not int or item_id <= 0 or session_id is None):
            raise ValueError("item_id requires a Session and a positive integer")
        with self._transaction() as db:
            self._owned(db, lease)
            db.execute("UPDATE tasks SET checkpoint=?,session_id=?,item_id=?,updated_at=? WHERE id=?",
                       (body,session_id,item_id,self.clock(),lease.task_id))
            self._record(db,lease.task_id,"checkpoint",lease.epoch,
                         {"session_id":session_id,"item_id":item_id,"value":value})

    def complete(self, lease, result):
        body = _json(result)
        with self._transaction() as db:
            old = db.execute("SELECT * FROM tasks WHERE id=?", (lease.task_id,)).fetchone()
            if (old and old["status"] == "completed" and old["owner"] == lease.owner
                    and old["epoch"] == lease.epoch and old["result"] == body):
                return self._decode(old)
            self._owned(db, lease)
            db.execute("UPDATE tasks SET status='completed',result=?,updated_at=? WHERE id=?",
                       (body,self.clock(),lease.task_id))
            self._record(db,lease.task_id,"completed",lease.epoch,{"result":result})
            return self._decode(db.execute("SELECT * FROM tasks WHERE id=?", (lease.task_id,)).fetchone())

    def fail(self, lease, error, *, retry_delay=30):
        _text(error,"error")
        if type(retry_delay) not in (int,float) or not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError("retry_delay must be finite and nonnegative")
        with self._transaction() as db:
            row = self._owned(db,lease)
            terminal = row["attempts"] >= row["max_attempts"]
            available = self.clock() + retry_delay * (2 ** (row["attempts"] - 1))
            db.execute('''UPDATE tasks SET status=?,last_error=?,available_at=?,lease_until=NULL,
                          updated_at=? WHERE id=?''',
                       ("failed" if terminal else "pending",error,available,self.clock(),lease.task_id))
            self._record(db,lease.task_id,"failed" if terminal else "retry_scheduled",lease.epoch,
                         {"error":error,"available_at":available})

    def get(self, task_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._decode(row)

    def history(self, task_id):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM transitions WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
        return [{**dict(row),"body":json.loads(row["body"])} for row in rows]

    def backup(self, destination):
        """Consistent SQLite snapshot, including task/checkpoint/transition state.

        External evidence/artifacts require the workload's coordinated backup.
        Restored leases remain fenced until their saved expiry; callers must
        not run a restored copy concurrently with the original deployment.
        """
        destination = Path(destination).resolve()
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open("xb"):
            pass
        source = self._connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

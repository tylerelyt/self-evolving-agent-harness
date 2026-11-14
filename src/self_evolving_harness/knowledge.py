"""Scoped immutable fact revisions and durable publication to a memory provider.

The local CAS head is authoritative. Remote search is an index: stale results
cannot resurrect revoked, expired, superseded or undelivered facts. Source
Session/Item identities are provenance supplied by a trusted evidence adapter;
this core validates their shape, not the original business observations.
"""

from contextlib import contextmanager
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Protocol
from uuid import uuid4

from .learning import canonical


def content_digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def validate_scope(scope):
    if not isinstance(scope, dict) or set(scope) != {'tenant', 'environment', 'service'}:
        raise ValueError('Memory requires an exact tenant/environment/service scope')
    if any(not isinstance(x, str) or not x.strip() or len(x) > 256 for x in scope.values()):
        raise ValueError('Memory scope values must be nonempty bounded strings')
    return json.loads(canonical(scope))


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Expected an immutable SHA-256 identity')
    return value


def _sources(sources):
    if not isinstance(sources, list) or not sources:
        raise ValueError('A fact requires original Session/Item/hash sources')
    for source in sources:
        if (not isinstance(source, dict) or not {'session_id', 'item_id', 'sha256'} <= set(source)
                or set(source) - {'session_id', 'item_id', 'sha256', 'evidence_path'}):
            raise ValueError('Each source must identify Session, Item and content hash')
        if not isinstance(source['session_id'], str) or not source['session_id'].strip():
            raise ValueError('Invalid source Session')
        if type(source['item_id']) is not int or source['item_id'] <= 0:
            raise ValueError('Invalid source Item')
        _hash(source['sha256'])
        if 'evidence_path' in source and (not isinstance(source['evidence_path'], str) or not source['evidence_path'].strip()):
            raise ValueError('Invalid evidence locator')
    if len({canonical(x) for x in sources}) != len(sources):
        raise ValueError('Duplicate memory provenance')
    return json.loads(canonical(sources))


class MemoryProvider(Protocol):
    def put_revision(self, record: dict) -> dict:
        """Idempotently publish immutable record; return URI/revision/hash receipt."""
        ...

    def search(self, scope: dict, query: str, limit: int = 10) -> list[dict]:
        """Return citation receipts within scope; no authority to select local heads."""
        ...


class RevisionConflict(ValueError):
    pass


class KnowledgeStore:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self.transaction() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('Unsupported knowledge schema')
            db.execute('''CREATE TABLE IF NOT EXISTS revisions (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, fact_key TEXT NOT NULL,
                parent TEXT, record TEXT NOT NULL, digest TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS heads (
                scope TEXT NOT NULL, fact_key TEXT NOT NULL, revision TEXT NOT NULL REFERENCES revisions(id),
                PRIMARY KEY(scope,fact_key))''')
            db.execute('''CREATE TABLE IF NOT EXISTS outbox (
                revision TEXT PRIMARY KEY REFERENCES revisions(id), status TEXT NOT NULL,
                owner TEXT, epoch INTEGER NOT NULL DEFAULT 0, lease_until REAL,
                available_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                receipt TEXT, last_error TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, revision TEXT NOT NULL,
                kind TEXT NOT NULL, body TEXT NOT NULL, recorded_at REAL NOT NULL)''')
            db.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, revision, kind, body):
        db.execute('INSERT INTO history(revision,kind,body,recorded_at) VALUES(?,?,?,?)',
                   (revision, kind, canonical(body), self.clock()))

    @staticmethod
    def _record(row):
        if row is None:
            return None
        record = json.loads(row['record'])
        if content_digest(record) != row['digest']:
            raise ValueError('Stored memory bytes changed')
        spec = {k: v for k, v in record.items() if k not in {'revision_id', 'created_at'}}
        if content_digest(spec) != row['id'] or record['revision_id'] != row['id']:
            raise ValueError('Stored memory revision identity changed')
        return record

    def head(self, scope, key):
        scope = canonical(validate_scope(scope))
        with self.transaction() as db:
            row = db.execute('''SELECT r.* FROM revisions r JOIN heads h ON h.revision=r.id
                WHERE h.scope=? AND h.fact_key=?''', (scope, key)).fetchone()
            return self._record(row)

    def get(self, revision_id):
        _hash(revision_id)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM revisions WHERE id=?', (revision_id,)).fetchone()
            if row is None:
                raise KeyError(revision_id)
            return self._record(row)

    def revise(self, *, scope, key, content, sources, expected_revision=None,
               valid_until=None, state='active'):
        scope = validate_scope(scope)
        if not isinstance(key, str) or not key.strip() or len(key) > 512:
            raise ValueError('Fact key must be a bounded nonempty string')
        if state not in ('active', 'revoked') or not isinstance(content, dict):
            raise ValueError('Expected fact content object and active/revoked state')
        if valid_until is not None and (type(valid_until) not in (int, float)
                or not math.isfinite(valid_until) or valid_until <= 0):
            raise ValueError('Invalid memory validity time')
        if expected_revision is not None:
            _hash(expected_revision)
        spec = {'schema_version': 1, 'scope': scope, 'key': key, 'content': content,
                'sources': _sources(sources), 'parent_revision': expected_revision,
                'valid_until': valid_until, 'state': state}
        identifier = content_digest(spec)
        with self.transaction() as db:
            current = db.execute('SELECT revision FROM heads WHERE scope=? AND fact_key=?',
                                 (canonical(scope), key)).fetchone()
            current_id = current['revision'] if current else None
            if current_id == identifier:
                return self._record(db.execute('SELECT * FROM revisions WHERE id=?', (identifier,)).fetchone())
            if current_id != expected_revision:
                raise RevisionConflict('Fact head changed; re-read before revising')
            if expected_revision is not None:
                parent = self._record(db.execute('SELECT * FROM revisions WHERE id=?', (expected_revision,)).fetchone())
                if parent is None or parent['scope'] != scope or parent['key'] != key:
                    raise RevisionConflict('A revision cannot cross its source fact scope')
            record = {**spec, 'revision_id': identifier, 'created_at': self.clock()}
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
                       (identifier, canonical(scope), key, expected_revision, canonical(record), content_digest(record)))
            db.execute('INSERT INTO heads VALUES(?,?,?) ON CONFLICT(scope,fact_key) DO UPDATE SET revision=excluded.revision',
                       (canonical(scope), key, identifier))
            db.execute("INSERT INTO outbox(revision,status,available_at) VALUES(?,'pending',?)", (identifier, self.clock()))
            self.event(db, identifier, 'revised', {'parent_revision': expected_revision, 'state': state})
            return record

    def outbox(self):
        with self.transaction() as db:
            result = [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY rowid')]
            for row in result:
                row['receipt'] = json.loads(row['receipt']) if row['receipt'] else None
            return result

    def pending(self, scope=None):
        expected = canonical(validate_scope(scope)) if scope is not None else None
        with self.transaction() as db:
            rows = db.execute("SELECT r.scope FROM outbox o JOIN revisions r ON o.revision=r.id WHERE o.status!='delivered'").fetchall()
            return sum(expected is None or row['scope'] == expected for row in rows)

    def _claim(self, owner, lease_seconds):
        with self.transaction() as db:
            now = self.clock()
            row = db.execute('''SELECT * FROM outbox WHERE available_at<=? AND
                (status='pending' OR (status='sending' AND lease_until<=?)) ORDER BY rowid LIMIT 1''', (now, now)).fetchone()
            if row is None:
                return None
            epoch = row['epoch'] + 1
            db.execute("UPDATE outbox SET status='sending',owner=?,epoch=?,lease_until=?,attempts=attempts+1 WHERE revision=?",
                       (owner, epoch, now + lease_seconds, row['revision']))
            self.event(db, row['revision'], 'publication_intent', {'owner': owner, 'epoch': epoch})
            return row['revision'], epoch, row['attempts'] + 1

    @staticmethod
    def _receipt(record, receipt):
        if (not isinstance(receipt, dict) or not isinstance(receipt.get('uri'), str)
                or not receipt['uri'].strip() or receipt.get('revision_id') != record['revision_id']
                or receipt.get('content_sha256') != content_digest(record)):
            raise ValueError('Memory provider citation does not match immutable revision bytes')
        return {'uri': receipt['uri'], 'revision_id': receipt['revision_id'],
                'content_sha256': receipt['content_sha256']}

    def drain(self, provider, *, owner='memory-worker', limit=10, lease_seconds=120, retry_delay=2):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError('Publication requires an owner')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid publication limit')
        if (type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or lease_seconds <= 0
                or type(retry_delay) not in (int, float) or not math.isfinite(retry_delay) or retry_delay < 0):
            raise ValueError('Invalid memory lease or retry delay')
        results = []
        for _ in range(limit):
            claim = self._claim(owner, lease_seconds)
            if claim is None:
                break
            identifier, epoch, attempts = claim
            record = self.get(identifier)
            try:
                receipt = self._receipt(record, provider.put_revision(json.loads(canonical(record))))
            except Exception as error:
                with self.transaction() as db:
                    changed = db.execute("""UPDATE outbox SET status='pending',available_at=?,last_error=?
                        WHERE revision=? AND status='sending' AND owner=? AND epoch=? AND lease_until>?""",
                        (self.clock() + retry_delay * 2 ** min(attempts - 1, 10), type(error).__name__,
                         identifier, owner, epoch, self.clock())).rowcount
                    if changed:
                        self.event(db, identifier, 'publication_failed', {'error_type': type(error).__name__})
                results.append({'revision_id': identifier, 'status': 'retry', 'error_type': type(error).__name__})
                continue
            with self.transaction() as db:
                changed = db.execute("""UPDATE outbox SET status='delivered',receipt=?,last_error=NULL
                    WHERE revision=? AND status='sending' AND owner=? AND epoch=? AND lease_until>?""",
                    (canonical(receipt), identifier, owner, epoch, self.clock())).rowcount
                if not changed:
                    raise RuntimeError('Memory publication lease lost; receipt must be reconciled idempotently')
                self.event(db, identifier, 'published', receipt)
            results.append({'revision_id': identifier, 'status': 'delivered', **receipt})
        return results

    def search(self, provider, *, scope, query, limit=10):
        scope = validate_scope(scope)
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Memory search needs a nonempty query and bounded limit')
        hits = provider.search(json.loads(canonical(scope)), query, limit=limit)
        if not isinstance(hits, list):
            raise ValueError('Memory provider search must return citation records')
        facts, seen = [], set()
        with self.transaction() as db:
            for hit in hits:
                if not isinstance(hit, dict):
                    raise ValueError('Invalid memory citation')
                identifier = _hash(hit.get('revision_id'))
                if identifier in seen:
                    continue
                row = db.execute('''SELECT r.*,o.receipt,o.status FROM revisions r
                    JOIN heads h ON h.revision=r.id JOIN outbox o ON o.revision=r.id WHERE r.id=?''', (identifier,)).fetchone()
                if row is None:
                    continue  # Unknown/superseded remote index hits convey no authority.
                record = self._record(row)
                if record['scope'] != scope:
                    raise PermissionError('Memory provider returned a fact outside the authorized scope')
                if (record['state'] != 'active' or row['status'] != 'delivered'
                        or record['valid_until'] is not None and record['valid_until'] <= self.clock()):
                    continue
                receipt = self._receipt(record, hit)
                if receipt != json.loads(row['receipt']):
                    raise ValueError('Memory citation changed after publication')
                seen.add(identifier)
                facts.append({'revision': record, 'citation': receipt})
                if len(facts) >= limit:
                    break
        return {'historical_only': True, 'facts': facts}

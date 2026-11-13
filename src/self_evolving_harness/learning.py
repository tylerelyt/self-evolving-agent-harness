"""Persistent candidate/evaluation/publication ledger with explicit transitions.

This is a local provenance ledger, not an authorization service or distributed
job scheduler. Publication intents survive failure but do not retry side effects.
"""

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from .evaluation import release_allowed


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("Expected a SHA-256 content identity")
    return value


class LearningLedger:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("Unsupported learning ledger schema")
            db.execute("""CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                evaluation TEXT, publication TEXT, adoption TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at REAL NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES candidates(id))""")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('namespace',?)", (uuid4().hex,))
            db.execute("PRAGMA user_version=2")

    @property
    def namespace(self):
        """A stable local workspace identity, independent of its filesystem path."""
        with self._transaction() as db:
            return db.execute("SELECT value FROM metadata WHERE key='namespace'").fetchone()[0]

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, candidate_id, kind, payload):
        db.execute("INSERT INTO history(candidate_id,kind,payload,recorded_at) VALUES(?,?,?,?)",
                   (candidate_id, kind, canonical(payload), time.time()))

    @staticmethod
    def _get(db, candidate_id):
        row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return dict(row)

    def register(self, *, scope, artifact_sha256, parent_sha256, sources, generator, generation=None):
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("A candidate requires an explicit scope")
        if not sources or not all(isinstance(x, str) and x.strip() for x in sources):
            raise ValueError("A candidate requires source references")
        if not isinstance(generator, str) or not generator.strip():
            raise ValueError("A candidate requires a generator identity")
        spec = {"schema_version": 1, "kind": "skill", "scope": scope,
                "artifact_sha256": _hash(artifact_sha256), "parent_sha256": _hash(parent_sha256),
                "sources": sorted(set(sources)), "generator": generator}
        if generation is not None:
            if not isinstance(generation, dict):
                raise ValueError("Generation context must be an object")
            spec["generation"] = generation
        candidate_id = digest(spec)
        with self._transaction() as db:
            existing = db.execute("SELECT spec FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO candidates(id,spec,status) VALUES(?,?,?)",
                           (candidate_id, canonical(spec), "proposed"))
                self._event(db, candidate_id, "proposed", spec)
            elif existing["spec"] != canonical(spec):
                raise ValueError("Candidate identity conflict")
        return candidate_id

    def evaluate(self, candidate_id, report):
        encoded = canonical(report)
        with self._transaction() as db:
            row = self._get(db, candidate_id)
            spec = json.loads(row["spec"])
            if (report.get("candidate_sha256") != spec["artifact_sha256"]
                    or report.get("baseline_sha256") != spec["parent_sha256"]):
                raise ValueError("Evaluation does not match the candidate and parent")
            if not isinstance(report.get("passed"), bool) or not report.get("cases"):
                raise ValueError("An evaluation needs an explicit result and nonempty cases")
            if report["passed"] and not release_allowed(
                report["cases"], spec["parent_sha256"], spec["artifact_sha256"]
            ):
                raise ValueError("Evaluation approval lacks complete improvement evidence")
            if row["evaluation"] is not None:
                if row["evaluation"] != encoded:
                    raise ValueError("An evaluated candidate cannot silently change its report")
                return digest(report)
            if row["status"] != "proposed":
                raise ValueError("Candidate is not awaiting evaluation")
            status = "eligible" if report["passed"] else "rejected"
            db.execute("UPDATE candidates SET status=?,evaluation=? WHERE id=?",
                       (status, encoded, candidate_id))
            self._event(db, candidate_id, status, {"report_sha256": digest(report)})
        return digest(report)

    def begin_publication(self, candidate_id, receivers):
        if not receivers or not all(isinstance(x, str) and x.strip() for x in receivers):
            raise ValueError("Publication needs explicit receiving instances")
        payload = {"receivers": sorted(set(receivers))}
        with self._transaction() as db:
            row = self._get(db, candidate_id)
            if row["status"] != "eligible":
                raise ValueError("Publication requires a new eligible candidate")
            db.execute("UPDATE candidates SET status='publishing',publication=? WHERE id=?",
                       (canonical({"intent": payload}), candidate_id))
            self._event(db, candidate_id, "publication_intent", payload)

    def published(self, candidate_id, receipt):
        with self._transaction() as db:
            row = self._get(db, candidate_id)
            spec = json.loads(row["spec"])
            publication = json.loads(row["publication"] or "{}")
            if "receipt" in publication:
                if publication["receipt"] != receipt:
                    raise ValueError("Publication receipt conflict")
                return
            if row["status"] != "publishing" or receipt.get("sha256") != spec["artifact_sha256"]:
                raise ValueError("Receipt does not match a pending publication")
            expected = set(publication["intent"]["receivers"])
            received = receipt.get("receivers", {})
            if (set(received) != expected or receipt.get("receiver_errors")
                    or any(value.get("sha256") != spec["artifact_sha256"] for value in received.values())):
                raise ValueError("Publication is incomplete; retain its pending intent")
            publication["receipt"] = receipt
            db.execute("UPDATE candidates SET status='published',publication=? WHERE id=?",
                       (canonical(publication), candidate_id))
            self._event(db, candidate_id, "published", receipt)

    def adopted(self, candidate_id, receipts):
        with self._transaction() as db:
            row = self._get(db, candidate_id)
            encoded = canonical(receipts)
            if row["adoption"] is not None:
                if row["adoption"] != encoded:
                    raise ValueError("Adoption receipt conflict")
                return
            if row["status"] != "published":
                raise ValueError("Adoption requires verified publication")
            spec = json.loads(row["spec"])
            expected = set(json.loads(row["publication"])["intent"]["receivers"])
            if set(receipts) != expected or any(
                entry.get("skill_sha256") != spec["artifact_sha256"]
                or entry.get("verified") is not True or not entry.get("session_id")
                or not entry.get("report_item_id") for entry in receipts.values()
            ):
                raise ValueError("Adoption lacks per-instance task evidence")
            db.execute("UPDATE candidates SET status='adopted',adoption=? WHERE id=?", (encoded, candidate_id))
            self._event(db, candidate_id, "adopted", receipts)

    def get(self, candidate_id):
        with self._transaction() as db:
            row = self._get(db, candidate_id)
            return {**row, **{key: json.loads(row[key]) if row[key] else None
                            for key in ("spec", "evaluation", "publication", "adoption")}}

    def candidates(self, *, statuses=None):
        with self._transaction() as db:
            query = "SELECT id,status,spec FROM candidates ORDER BY rowid"
            values = ()
            if statuses is not None:
                values = tuple(statuses)
                if not values:
                    return []
                query = "SELECT id,status,spec FROM candidates WHERE status IN (" + ",".join("?" for _ in values) + ") ORDER BY rowid"
            return [{**dict(row), "spec": json.loads(row["spec"])} for row in db.execute(query, values)]

    def recovered(self, candidate_id, restored_sha256, receipt):
        """Record verified restoration; never execute or retry a side effect."""
        _hash(restored_sha256)
        if not isinstance(receipt, dict) or receipt.get("verified") is not True:
            raise ValueError("Recovery requires verified restoration evidence")
        with self._transaction() as db:
            row = self._get(db, candidate_id)
            if row["status"] == "rolled_back":
                previous = db.execute(
                    "SELECT payload FROM history WHERE candidate_id=? AND kind='rolled_back' ORDER BY seq DESC LIMIT 1",
                    (candidate_id,),
                ).fetchone()
                if json.loads(previous["payload"])["restored_sha256"] != restored_sha256:
                    raise ValueError("Recovery target conflict")
                return
            if row["status"] not in ("publishing", "published", "adopted"):
                raise ValueError("Candidate has no publication to recover")
            if restored_sha256 != json.loads(row["spec"])["parent_sha256"]:
                raise ValueError("Recovery does not restore the evaluated parent")
            db.execute("UPDATE candidates SET status='rolled_back' WHERE id=?", (candidate_id,))
            self._event(db, candidate_id, "rolled_back", {"restored_sha256": restored_sha256, "receipt": receipt})

    def history(self, candidate_id):
        with self._transaction() as db:
            self._get(db, candidate_id)
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in db.execute(
                "SELECT * FROM history WHERE candidate_id=? ORDER BY seq", (candidate_id,))]

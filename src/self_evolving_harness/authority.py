"""Scoped approval and at-most-once dispatch for trusted tool adapters.

Principals come from an authenticated transport, never from request JSON. Unknown
outcomes are reconciled against the tool's own state; they are never retried here.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_spec(value):
    fields = {"tenant", "environment", "incident", "action", "parameters", "evidence_sha256", "capability_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("An action requires exactly the scoped action/evidence/version fields")
    for name in fields - {"parameters"}:
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValueError(f"Invalid {name}")
    for name in ("evidence_sha256", "capability_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", value[name]):
            raise ValueError(f"Invalid {name}")
    if not isinstance(value["parameters"], dict):
        raise ValueError("Action parameters must be an object")
    return json.loads(canonical(value))


@dataclass(frozen=True)
class Principal:
    name: str
    roles: frozenset
    grants: frozenset  # exact (tenant, environment, action) triples, no wildcard

    def __post_init__(self):
        if (not isinstance(self.name, str) or not self.name.strip()
                or not self.roles.issubset({"approve", "execute"})
                or any(not isinstance(scope, tuple) or len(scope) != 3
                       or any(not isinstance(x, str) or not x.strip() for x in scope) for scope in self.grants)):
            raise ValueError("A principal requires explicit roles and exact scope triples")

    def require(self, role, spec):
        if not self.name or role not in self.roles or (
            spec["tenant"], spec["environment"], spec["action"]
        ) not in self.grants:
            raise PermissionError("Principal lacks this role or exact action scope")


class ApprovalAuthority:
    def __init__(self, key, *, clock=time.time, max_ttl=300, audience=None):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("Approval signing requires at least 32 private key bytes")
        self.key, self.clock, self.max_ttl = key, clock, max_ttl
        self.audience = audience

    def issue(self, approver, executor, spec, *, ttl=120):
        if not self.audience:
            raise ValueError("Use the authority bound to an ActionJournal")
        spec = validate_spec(spec)
        approver.require("approve", spec)
        executor.require("execute", spec)
        if type(ttl) not in (float, int) or not math.isfinite(ttl) or not 0 < ttl <= self.max_ttl:
            raise ValueError("Approval lifetime exceeds policy")
        now = self.clock()
        body = {"schema": 1, "audience": self.audience, "nonce": uuid4().hex, "approver": approver.name,
                "executor": executor.name, "issued_at": now, "expires_at": now + ttl, "spec": spec}
        return {"body": body, "signature": hmac.new(self.key, canonical(body).encode(), hashlib.sha256).hexdigest()}

    def verify(self, token, executor, spec):
        spec = validate_spec(spec)
        executor.require("execute", spec)
        if not isinstance(token, dict) or set(token) != {"body", "signature"}:
            raise PermissionError("Invalid approval token")
        expected = hmac.new(self.key, canonical(token["body"]).encode(), hashlib.sha256).hexdigest()
        if not isinstance(token["signature"], str) or not hmac.compare_digest(expected, token["signature"]):
            raise PermissionError("Invalid approval signature")
        body = token["body"]
        try:
            valid = (body["schema"] == 1 and body["audience"] == self.audience and body["executor"] == executor.name and body["spec"] == spec
                     and body["issued_at"] <= self.clock() < body["expires_at"]
                     and body["expires_at"] - body["issued_at"] <= self.max_ttl)
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise PermissionError("Approval expired or not bound to this executor, action, evidence and version")
        return body


class ActionJournal:
    def __init__(self, path, authority):
        self.path, self.authority = Path(path).resolve(), authority
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported action journal schema")
            db.execute("""CREATE TABLE IF NOT EXISTS actions (
                operation TEXT PRIMARY KEY, spec TEXT NOT NULL, executor TEXT NOT NULL,
                approval TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT, updated REAL NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS approvals (nonce TEXT PRIMARY KEY, operation TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('audience',?)", (uuid4().hex,))
            audience = db.execute("SELECT value FROM metadata WHERE key='audience'").fetchone()[0]
            db.execute("PRAGMA user_version=1")
        self.authority = ApprovalAuthority(authority.key, clock=authority.clock,
                                           max_ttl=authority.max_ttl, audience=audience)

    @contextmanager
    def transaction(self):
        with sqlite3.connect(self.path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def get(self, operation):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM actions WHERE operation=?", (operation,)).fetchone()
        if row is None:
            raise KeyError(operation)
        result = dict(row)
        for key in ("spec", "approval", "receipt"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    @contextmanager
    def owner(self, operation):
        locks = self.path.parent / (self.path.name + ".owners")
        locks.mkdir(exist_ok=True)
        identity = hashlib.sha256(operation.encode()).hexdigest()
        with (locks / identity).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def dispatch(self, operation, executor, spec, approval, apply):
        if not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation):
            raise ValueError("Operation must be a bounded idempotency key")
        self.authority.verify(approval, executor, spec)
        with self.owner(operation):
            return self._dispatch(operation, executor, spec, approval, apply)

    def _dispatch(self, operation, executor, spec, approval, apply):
        if not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation):
            raise ValueError("Operation must be a bounded idempotency key")
        spec = validate_spec(spec)
        body = self.authority.verify(approval, executor, spec)
        with self.transaction() as db:
            old = db.execute("SELECT spec,executor FROM actions WHERE operation=?", (operation,)).fetchone()
            if old:
                if old["spec"] != canonical(spec) or old["executor"] != executor.name:
                    raise PermissionError("Idempotency key already belongs to another action")
                existing = True
            else:
                prior = db.execute("SELECT operation FROM approvals WHERE nonce=?", (body["nonce"],)).fetchone()
                if prior:
                    raise PermissionError("Approval has already authorized another operation")
                db.execute("INSERT INTO approvals VALUES(?,?)", (body["nonce"], operation))
                db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?)", (
                    operation, canonical(spec), executor.name, canonical(approval), "unknown", None, time.time()))
                existing = False
        if existing:
            return self.get(operation)  # never repeat a side effect, even after a crash
        receipt = apply(operation, spec)  # an exception leaves the durable unknown intent
        if not isinstance(receipt, dict) or not receipt or receipt.get("operation") != operation:
            raise ValueError("Tool must return a nonempty receipt bound to the operation")
        encoded = canonical(receipt)
        with self.transaction() as db:
            db.execute("UPDATE actions SET state='completed',receipt=?,updated=? WHERE operation=? AND state='unknown'",
                       (encoded, time.time(), operation))
        return self.get(operation)

    def reconcile(self, operation, principal, observe):
        record = self.get(operation)
        principal.require("approve", record["spec"])
        with self.owner(operation):
            return self._reconcile(operation, principal, observe)

    def _reconcile(self, operation, principal, observe):
        record = self.get(operation)
        principal.require("approve", record["spec"])
        if record["state"] != "unknown":
            return record
        outcome = observe(operation, record["spec"])
        if (not isinstance(outcome, dict) or outcome.get("operation") != operation
                or outcome.get("state") not in ("completed", "not_applied", "unknown")):
            raise ValueError("Reconciliation needs independently observed operation state")
        with self.transaction() as db:
            db.execute("UPDATE actions SET state=?,receipt=?,updated=? WHERE operation=? AND state='unknown'",
                       (outcome["state"], canonical(outcome), time.time(), operation))
        return self.get(operation)

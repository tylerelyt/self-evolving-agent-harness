"""Scoped effects: real HTTP/process behavior, interrupted outcomes and ownership."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from self_evolving_harness.authority import ActionJournal, ApprovalAuthority, Principal
from apps.dedicated.__main__ import ToolGateway, make_server


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.key = os.urandom(32)
        self.authority = ApprovalAuthority(self.key, clock=lambda: self.now)
        self.journal = ActionJournal(self.root / "actions.sqlite3", self.authority)
        self.authority = self.journal.authority
        grants = frozenset({("acme", "staging", "restart")})
        self.operator = Principal("engineer", frozenset({"approve"}), grants)
        self.worker = Principal("oncall", frozenset({"execute"}), grants)
        self.spec = {"tenant": "acme", "environment": "staging", "incident": "incident-1",
                     "action": "restart", "parameters": {"service": "api"},
                     "evidence_sha256": "a" * 64, "capability_sha256": "b" * 64}
        self.token = self.authority.issue(self.operator, self.worker, self.spec)

    def apply(self, operation, spec):
        return {"operation": operation, "external_receipt": "restart-observed"}

    def test_token_is_bound_to_scope_parameters_evidence_version_and_executor(self):
        for key, value in (("tenant", "other"), ("environment", "production"), ("incident", "incident-2"),
                           ("parameters", {"service": "db"}), ("evidence_sha256", "c" * 64),
                           ("capability_sha256", "d" * 64)):
            with self.subTest(field=key), self.assertRaises(PermissionError):
                self.journal.dispatch("restart-1", self.worker, {**self.spec, key: value}, self.token, self.apply)
        impostor = Principal("other", self.worker.roles, self.worker.grants)
        with self.assertRaises(PermissionError):
            self.authority.verify(self.token, impostor, self.spec)
        self.assertRaises(KeyError, self.journal.get, "restart-1")

    def test_approval_cannot_cross_journals_even_with_the_same_signing_key(self):
        other = ActionJournal(self.root / "other.sqlite3", ApprovalAuthority(self.key, clock=lambda: self.now))
        with self.assertRaises(PermissionError):
            other.dispatch("restart-1", self.worker, self.spec, self.token, self.apply)

    def test_forgery_expiry_and_role_checks_fail_closed(self):
        bad = json.loads(json.dumps(self.token))
        bad["body"]["expires_at"] += 3600
        with self.assertRaises(PermissionError):
            self.authority.verify(bad, self.worker, self.spec)
        self.now = self.token["body"]["expires_at"]
        with self.assertRaises(PermissionError):
            self.authority.verify(self.token, self.worker, self.spec)
        with self.assertRaises(PermissionError):
            self.authority.issue(self.worker, self.worker, self.spec)
        with self.assertRaises(ValueError):
            self.authority.issue(self.operator, self.worker, self.spec, ttl=float("inf"))

    def test_concurrent_dispatch_applies_once_and_approval_cannot_be_replayed(self):
        calls = []
        def apply(operation, spec):
            calls.append(operation)
            time.sleep(.03)
            return self.apply(operation, spec)
        def dispatch(_index):
            return self.journal.dispatch("restart-1", self.worker, self.spec, self.token, apply)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(dispatch, range(6)))
        self.assertEqual(calls, ["restart-1"])
        self.assertTrue(all(result["state"] == "completed" for result in results))
        with self.assertRaises(PermissionError):
            self.journal.dispatch("restart-2", self.worker, self.spec, self.token, apply)
        changed = {**self.spec, "incident": "incident-2"}
        new = self.authority.issue(self.operator, self.worker, changed)
        with self.assertRaises(PermissionError):
            self.journal.dispatch("restart-1", self.worker, changed, new, apply)

    def test_real_child_crash_leaves_unknown_and_never_repeats_effect(self):
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({"key": self.key.hex(), "spec": self.spec, "token": self.token}))
        script = '''
import json,os,sys
from pathlib import Path
from self_evolving_harness.authority import *
p=Path(sys.argv[1]); x=json.loads((p/'fixture.json').read_text())
a=ApprovalAuthority(bytes.fromhex(x['key']),clock=lambda:1000)
j=ActionJournal(p/'actions.sqlite3',a)
w=Principal('oncall',frozenset({'execute'}),frozenset({('acme','staging','restart')}))
def apply(op,spec):
 (p/'external-effect').write_text(op)
 os._exit(17)
j.dispatch('restart-1',w,x['spec'],x['token'],apply)
'''
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], env=env, timeout=10)
        self.assertEqual(result.returncode, 17)
        self.assertEqual((self.root / "external-effect").read_text(), "restart-1")
        reopened = ActionJournal(self.journal.path, self.authority)
        def must_not_apply(*_args):
            self.fail("Unknown outcome was blindly repeated")
        self.assertEqual(reopened.dispatch("restart-1", self.worker, self.spec, self.token, must_not_apply)["state"], "unknown")
        actual = reopened.reconcile("restart-1", self.operator,
                                    lambda op, spec: {"operation": op, "state": "completed",
                                                      "evidence": (self.root / "external-effect").read_text()})
        self.assertEqual(actual["state"], "completed")

    def test_reconciliation_cannot_race_inflight_effect(self):
        started, proceed = threading.Event(), threading.Event()
        observations = []
        def apply(op, spec):
            started.set()
            proceed.wait(3)
            return self.apply(op, spec)
        def observe(op, spec):
            observations.append(op)
            return {"operation": op, "state": "not_applied"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            effect = pool.submit(self.journal.dispatch, "restart-1", self.worker, self.spec, self.token, apply)
            self.assertTrue(started.wait(3))
            read = pool.submit(self.journal.reconcile, "restart-1", self.operator, observe)
            time.sleep(.03)
            proceed.set()
            self.assertEqual(effect.result()["state"], "completed")
            self.assertEqual(read.result()["state"], "completed")
        self.assertEqual(observations, [])

    def test_malformed_receipt_keeps_unknown_and_wrong_role_cannot_reconcile(self):
        with self.assertRaises(ValueError):
            self.journal.dispatch("restart-1", self.worker, self.spec, self.token, lambda *_: {})
        self.assertEqual(self.journal.get("restart-1")["state"], "unknown")
        with self.assertRaises(PermissionError):
            self.journal.reconcile("restart-1", self.worker, lambda *_: {})

    def test_real_http_auth_and_pinned_tool_command(self):
        tool = self.root / "tool.py"
        tool.write_text('''import json,sys
from pathlib import Path
x=json.load(sys.stdin); p=Path(__file__).with_name('effects.json')
rows=json.loads(p.read_text()) if p.exists() else []
if sys.argv[1]=='apply':
 rows.append(x['operation']); p.write_text(json.dumps(rows))
print(json.dumps({'operation':x['operation'],'state':'completed' if x['operation'] in rows else 'not_applied','count':len(rows)}))
''')
        principals = [{"name": p.name, "roles": list(p.roles), "grants": [list(x) for x in p.grants],
                       "token_env": "TEST_" + p.name.upper()} for p in (self.operator, self.worker)]
        config = {"schema_version": 1, "signing_key_env": "TEST_SIGNING", "journal": "http.sqlite3",
                  "principals": principals,
                  "tools": {"restart": {"apply": [sys.executable, str(tool), "apply"],
                                         "observe": [sys.executable, str(tool), "observe"]}}}
        path = self.root / "gateway.json"
        path.write_text(json.dumps(config))
        env = {"TEST_SIGNING": "s" * 32, "TEST_ENGINEER": "e" * 32, "TEST_ONCALL": "w" * 32}
        with patch.dict(os.environ, env):
            gateway = ToolGateway(path)
        server = make_server(gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def post(route, body, token):
            request = Request(f"http://127.0.0.1:{server.server_port}" + route,
                              json.dumps(body).encode(), {"Authorization": "Bearer " + token,
                                                        "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                return json.load(response)
        with self.assertRaises(HTTPError) as failure:
            post("/v1/approvals", {"executor": "oncall", "spec": self.spec}, "bad")
        self.assertEqual(failure.exception.code, 403)
        approval = post("/v1/approvals", {"executor": "oncall", "spec": self.spec}, "e" * 32)
        body = {"operation": "real-http-1", "spec": self.spec, "approval": approval}
        first = post("/v1/actions", body, "w" * 32)
        again = post("/v1/actions", body, "w" * 32)
        self.assertEqual(first["state"], "completed")
        self.assertEqual(again["receipt"]["count"], 1)
        self.assertEqual(json.loads((self.root / "effects.json").read_text()), ["real-http-1"])
        body["spec"] = {**self.spec, "tenant": "competitor"}
        with self.assertRaises(HTTPError) as failure:
            post("/v1/actions", body, "w" * 32)
        self.assertEqual(failure.exception.code, 403)


if __name__ == "__main__":
    unittest.main()

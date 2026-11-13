"""Real HTTP restart and opt-in native service persistence acceptance."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from apps.oncall import service
from environment import get_snapshot,load_json,write_json


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def start(self,command,log):
        output=open(log,"a")
        child=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        def stop():
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid,signal.SIGKILL)
                    child.wait()
            output.close()
        self.addCleanup(stop)
        return child

    def wait(self,predicate,child,timeout=45):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if child.poll() is not None:
                self.fail(f"Runtime exited: {child.returncode}; logs under {self.root}")
            try:
                result=predicate()
                if result:return result
            except (FileNotFoundError,KeyError,json.JSONDecodeError,sqlite3.Error,ConnectionError):pass
            time.sleep(.1)
        self.fail(f"Timed out; logs under {self.root}")

    def test_environment_restart_preserves_scope_and_fault_and_rejects_two_owners(self):
        env=self.root/"environment"
        command=[sys.executable,"-B",str(ROOT/"apps/oncall/patrol/environment.py"),"--state",str(env),"serve","--resume"]
        first=self.start(command,self.root/"env.log")
        self.wait(lambda:(env/"environment.json").exists(),first)
        identity=get_snapshot(env,"orders")["environment_id"]
        (env/"orders.fault").write_text("on")
        self.wait(lambda:list((env/"alerts/orders").glob("*.json")),first)
        alert_ids={p.name for p in (env/"alerts/orders").glob("*.json")}
        duplicate=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=10)
        self.assertNotEqual(duplicate.returncode,0)
        self.assertIn("owner",duplicate.stderr)
        first.kill()  # Leave the stale manifest, as a host/runtime crash would.
        first.wait(timeout=10)
        second=self.start(command,self.root/"env.log")
        self.wait(lambda:load_json(env/"environment-process.json")["pid"]==second.pid,second)
        current=get_snapshot(env,"orders")
        self.assertEqual(current["environment_id"],identity)
        self.assertEqual((env/"orders.fault").read_text(),"on")
        self.assertLess(current["requests"],20) # New observation window, same scope.
        time.sleep(.7)
        self.assertEqual({p.name for p in (env/"alerts/orders").glob("*.json")},alert_ids)

    def test_monitor_rejects_snapshot_from_a_different_bound_environment(self):
        from unittest.mock import Mock,patch
        from environment import monitor_alerts
        write_json(self.root / "environment-scope.json",{"environment_id":"expected"})
        (self.root / "payments.fault").touch()
        stop=Mock()
        stop.wait.side_effect=[False,True]
        with patch("environment.get_snapshot",return_value={"service":"payments",
                   "environment_id":"other","error_rate":.5}):
            monitor_alerts(self.root,stop)
        self.assertFalse((self.root / "alerts").exists())
        self.assertFalse((self.root / "monitor-state.json").exists())

    def test_service_binding_change_is_rejected_before_any_child_start(self):
        root=self.root/"service"
        root.mkdir()
        binding={"configuration":{"mode":"different"},"environment_ids":{}}
        write_json(root/"service-binding.json",binding)
        with self.assertRaisesRegex(ValueError,"bindings"):
            service.serve(root,False,service.HERE/"workflow.py",resume=True)
        self.assertFalse((root/"service.json").exists())

    def test_service_lifetime_lock_excludes_a_second_process(self):
        code="""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from apps.oncall.service import service_owner
try:
 with service_owner(Path(sys.argv[2])): pass
except RuntimeError: sys.exit(23)
"""
        with service.service_owner(self.root):
            child=subprocess.run([sys.executable,"-B","-c",code,str(ROOT),str(self.root)],timeout=10)
        self.assertEqual(child.returncode,23)

    @unittest.skipUnless(os.environ.get("RSI_NATIVE_RESUME_TEST")=="1","opt-in pinned Hermes/SkillClaw acceptance")
    def test_native_service_restart_keeps_sessions_patrols_pages_and_active_skill(self):
        python=ROOT/".deps/hermes-agent/.venv/bin/python"
        root=self.root/"native"
        command=[str(python),"-B","-m","apps.oncall","serve","--out",str(root),"--resume","--patrol-only","--no-learning"]
        first=self.start(command,self.root/"service.log")
        def counts():
            values={}
            for alias in service.ALIASES:
                dbpath=root/"patrols"/alias/"patrol.sqlite"
                with sqlite3.connect(dbpath) as db:
                    values[alias]=db.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
            return values if min(values.values())>=2 else None
        self.wait(counts,first,timeout=60)
        first.send_signal(signal.SIGINT)
        first.wait(timeout=30)
        before=counts()
        scope=load_json(root/"service-binding.json")["environment_ids"]
        page=root/"patrols/instance-a/preserved-page.json"
        page.write_text('{"approval":"pending"}')
        approved=root/"approved-fixture.md"
        approved.write_bytes(service.SEED.read_bytes()+b"\nPreapproved restart fixture.\n")
        digest=service.sha256(approved)
        install="""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from apps.oncall import service
r=Path(sys.argv[2]); source=Path(sys.argv[3]); digest=service.sha256(source)
service.publish(r,source,digest)
s=service.load_json(r/'learning/state.json'); s['active_sha256']=digest
service.write_json(r/'learning/state.json',s)
"""
        installed=subprocess.run([str(python),"-B","-c",install,str(ROOT),str(root),str(approved)],
                                 capture_output=True,text=True,timeout=30)
        self.assertEqual(installed.returncode,0,installed.stderr[-2000:])
        sessions={}
        for alias in service.ALIASES:
            with sqlite3.connect(root/"patrols"/alias/"hermes/state.db") as db:
                sessions[alias]={r[0] for r in db.execute("SELECT id FROM sessions")}
        second=self.start(command,self.root/"service.log")
        self.wait(lambda:load_json(root/"service.json").get("status")=="running",second,timeout=60)
        def advanced():
            current=counts()
            return current and all(current[a]>before[a] for a in before)
        self.wait(advanced,second,timeout=60)
        second.send_signal(signal.SIGINT)
        second.wait(timeout=30)
        self.assertEqual(load_json(root/"service-binding.json")["environment_ids"],scope)
        self.assertEqual(load_json(root/"learning/state.json")["active_sha256"],digest)
        self.assertEqual(page.read_text(),'{"approval":"pending"}')
        for alias in service.ALIASES:
            self.assertEqual(service.sha256(root/"receivers"/alias/service.ONCALL_SKILL/"SKILL.md"),digest)
            with sqlite3.connect(root/"patrols"/alias/"hermes/state.db") as db:
                current={r[0] for r in db.execute("SELECT id FROM sessions")}
            self.assertTrue(sessions[alias] <= current)


if __name__=="__main__":unittest.main()

"""Cross-process durable queue, fencing and restore acceptance."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT / "src"))
from self_evolving_harness.control import ControlStore, LeaseLost


def child_code(body):
    return "import sys,json,time,os\nsys.path.insert(0," + repr(str(ROOT / "src")) + ")\nfrom self_evolving_harness.control import ControlStore\n" + body


class ControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "control.sqlite3"
        self.now = 1000.0
        self.store = ControlStore(self.path,clock=lambda:self.now)

    def enqueue(self,**kwargs):
        return self.store.enqueue(scope="tenant/environment", key="incident-1",payload={"incident":"1"},**kwargs)

    def test_duplicate_alert_keeps_one_task_and_rejects_changed_input(self):
        args = dict(scope="t/e",source="monitor",event_id="alert-1",payload={"incident":"1"})
        first = self.store.event(**args)
        self.assertEqual(first,self.store.event(**args))
        with self.assertRaisesRegex(ValueError,"different"):
            self.store.event(**{**args,"payload":{"incident":"2"}})
        other = self.store.event(**{**args,"scope":"other-tenant/e"})
        self.assertNotEqual(first["id"],other["id"])
        self.assertEqual(len(self.store.history(first["id"])),1)

    def test_actual_processes_contend_for_one_owner(self):
        store = ControlStore(self.path)
        task = store.enqueue(scope="t/e",key="incident",payload={})
        start = self.root / "start"
        body = '''from pathlib import Path
s=ControlStore(sys.argv[1])
while not Path(sys.argv[2]).exists(): time.sleep(.005)
r=s.claim(str(os.getpid()),lease_seconds=30)
print(json.dumps(None if r is None else {"task":r[0].task_id,"owner":r[0].owner}),flush=True)
'''
        children = [subprocess.Popen([sys.executable,"-B","-c",child_code(body),str(self.path),str(start)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(5)]
        try:
            start.touch()
            results=[]
            for child in children:
                out,err=child.communicate(timeout=20)
                self.assertEqual(child.returncode,0,err)
                results.append(json.loads(out))
            winners=[row for row in results if row is not None]
            self.assertEqual(len(winners),1)
            self.assertEqual(winners[0]["task"],task["id"])
            self.assertEqual(store.get(task["id"])["attempts"],1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_stale_epoch_cannot_checkpoint_complete_heartbeat_or_fail(self):
        task=self.enqueue()
        old,_=self.store.claim("old",lease_seconds=2)
        self.store.checkpoint(old,{"step":"probe"},session_id="session-1",item_id=14)
        self.now+=3
        new,current=self.store.claim("new",lease_seconds=10)
        self.assertEqual(new.epoch,old.epoch+1)
        self.assertEqual(current["session_id"],"session-1")
        self.assertEqual(current["item_id"],14)
        for method in (lambda:self.store.checkpoint(old,{}),lambda:self.store.complete(old,{}),
                       lambda:self.store.heartbeat(old),lambda:self.store.fail(old,"late")):
            with self.assertRaises(LeaseLost):method()
        finished=self.store.complete(new,{"status":"resolved"})
        self.assertEqual(finished["status"],"completed")
        self.assertEqual(self.store.complete(new,{"status":"resolved"}),finished)
        with self.assertRaises(LeaseLost):self.store.complete(new,{"status":"different"})
        self.assertIsNone(self.store.claim("third"))
        self.assertEqual(self.store.get(task["id"])["attempts"],2)

    def test_real_worker_crash_retains_checkpoint_for_replacement(self):
        store=ControlStore(self.path)
        task=store.enqueue(scope="t/e",key="incident",payload={})
        code=child_code('''s=ControlStore(sys.argv[1])
lease,_=s.claim("doomed",lease_seconds=.15)
s.checkpoint(lease,{"tool_receipt":"receipt-4"},session_id="native-session",item_id=91)
os._exit(19)
''')
        child=subprocess.run([sys.executable,"-B","-c",code,str(self.path)],timeout=10)
        self.assertEqual(child.returncode,19)
        time.sleep(.2)
        replacement=ControlStore(self.path)
        lease,current=replacement.claim("replacement",lease_seconds=10)
        self.assertEqual(current["checkpoint"],{"tool_receipt":"receipt-4"})
        self.assertEqual((current["session_id"],current["item_id"]),("native-session",91))
        replacement.complete(lease,{"used_prior_checkpoint":True})
        self.assertEqual(store.get(task["id"])["status"],"completed")

    def test_learning_retry_backoff_survives_restart_and_exhausts(self):
        task=self.enqueue(queue="learning",max_attempts=2)
        lease,_=self.store.claim("learner",queue="learning",lease_seconds=10)
        self.store.fail(lease,"upstream unavailable",retry_delay=5)
        reopened=ControlStore(self.path,clock=lambda:self.now)
        self.assertIsNone(reopened.claim("learner-2",queue="learning"))
        self.now+=5
        new,_=reopened.claim("learner-2",queue="learning")
        reopened.fail(new,"still unavailable",retry_delay=5)
        self.assertEqual(reopened.get(task["id"])["status"],"failed")
        self.now+=500
        self.assertIsNone(reopened.claim("learner-3",queue="learning"))
        self.assertEqual([r["kind"] for r in reopened.history(task["id"])],
                         ["enqueued","claimed","retry_scheduled","claimed","failed"])

    def test_crashed_last_attempt_becomes_visible_terminal_failure(self):
        task=self.enqueue(max_attempts=1)
        self.store.claim("dead",lease_seconds=1)
        self.now+=2
        self.assertIsNone(self.store.claim("next"))
        self.assertEqual(self.store.get(task["id"])["status"],"failed")
        self.assertEqual(self.store.history(task["id"])[-1]["kind"],"exhausted")

    def test_backup_restore_has_consistent_task_checkpoint_and_history(self):
        task=self.enqueue()
        lease,_=self.store.claim("worker",lease_seconds=1)
        self.store.checkpoint(lease,{"state":"investigating"},session_id="s",item_id=7)
        backup=self.store.backup(self.root/"backup.sqlite3")
        restored=ControlStore(backup,clock=lambda:self.now)
        self.assertEqual(restored.get(task["id"]),self.store.get(task["id"]))
        self.assertEqual(restored.history(task["id"]),self.store.history(task["id"]))
        with self.assertRaises(FileExistsError):self.store.backup(backup)
        self.now+=2
        replacement,current=restored.claim("replacement")
        self.assertEqual(current["checkpoint"],{"state":"investigating"})
        restored.complete(replacement,{"recovered":True})
        self.assertEqual(self.store.get(task["id"])["status"],"running")

    def test_scope_and_queue_filters_do_not_claim_other_work(self):
        self.enqueue(queue="learning")
        self.assertIsNone(self.store.claim("runtime",queue="execution"))
        self.assertIsNone(self.store.claim("other",queue="learning",scope="other"))
        self.assertIsNotNone(self.store.claim("learner",queue="learning",scope="tenant/environment"))

    def test_invalid_values_are_rejected_before_state_write(self):
        for attempts in (True,0,-1):
            with self.assertRaises(ValueError):self.enqueue(max_attempts=attempts)
        self.enqueue()
        for duration in (True,0,float("nan"),float("inf")):
            with self.assertRaises(ValueError):self.store.claim("owner",lease_seconds=duration)
        lease,_=self.store.claim("owner")
        with self.assertRaises(ValueError):self.store.checkpoint(lease,{},item_id=1)
        with self.assertRaises(ValueError):self.store.checkpoint(lease,{},session_id="s",item_id=True)
        with self.assertRaises(ValueError):self.store.fail(lease,"x",retry_delay=float("nan"))
        self.assertEqual(self.store.get(lease.task_id)["status"],"running")


if __name__=="__main__":
    unittest.main()

"""Scoped knowledge CAS and actual process interruption at publication."""

from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
from self_evolving_harness.knowledge import KnowledgeStore, RevisionConflict, content_digest

SCOPE = {'tenant':'team','environment':'env-a','service':'orders'}
SOURCES = [{'session_id':'actual-source-session','item_id':17,'sha256':sha256(b'observed receipt').hexdigest()}]


class IndexFixture:
    """Explicit in-memory protocol fixture, never presented as OpenViking."""
    def __init__(self):
        self.records = {}
        self.receipts = {}

    def put_revision(self, record):
        identity = record['revision_id']
        if identity in self.records and self.records[identity] != record:
            raise ValueError('Immutable conflict')
        self.records[identity] = record
        result = {'revision_id':identity,'content_sha256':content_digest(record),'uri':'fixture://'+identity}
        self.receipts[identity] = result
        return result

    def search(self,scope,query,limit=10):
        return list(self.receipts.values())[:limit]


def revise_process(path, parent, value, gate, output):
    store=KnowledgeStore(path)
    gate.wait()
    try:
        result=store.revise(scope=SCOPE,key='diagnostic-route',content={'route':value},sources=SOURCES,expected_revision=parent)
        output.put(('success',result['revision_id']))
    except RevisionConflict:
        output.put(('conflict',None))


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.now=[100.0]
        self.store=KnowledgeStore(self.root/'knowledge.sqlite3',clock=lambda:self.now[0])
        self.index=IndexFixture()

    def revise(self, **changes):
        return self.store.revise(**{'scope':SCOPE,'key':'diagnostic-route','content':{'route':'/old'},'sources':SOURCES,**changes})

    def search(self,scope=SCOPE):
        return self.store.search(self.index,scope=scope,query='diagnostic route')

    def test_idempotent_source_preservation_cas_and_stale_index_filter(self):
        old=self.revise()
        self.assertEqual(self.revise(),old)
        self.store.drain(self.index)
        self.assertEqual(self.search()['facts'][0]['revision'],old)
        new=self.revise(content={'route':'/new'},expected_revision=old['revision_id'])
        self.assertEqual(self.search()['facts'],[])  # Old remote hit cannot revive superseded head.
        self.store.drain(self.index)
        self.assertEqual([x['revision']['revision_id'] for x in self.search()['facts']],[new['revision_id']])
        self.assertEqual(self.store.get(old['revision_id']),old)
        self.assertEqual(new['sources'],old['sources'])
        self.assertTrue(self.search()['historical_only'])
        with self.assertRaises(RevisionConflict):self.revise(content={'route':'/stale'},expected_revision=old['revision_id'])

    def test_expired_and_revoked_facts_are_not_retrieved(self):
        old=self.revise(valid_until=110)
        self.store.drain(self.index)
        self.now[0]=110
        self.assertEqual(self.search()['facts'],[])
        self.assertEqual(self.store.head(SCOPE,'diagnostic-route'),old)
        revoked=self.revise(expected_revision=old['revision_id'],state='revoked',content={'reason':'stale'})
        self.store.drain(self.index)
        self.assertEqual(self.search()['facts'],[])
        self.assertEqual(self.store.get(revoked['revision_id'])['parent_revision'],old['revision_id'])

    def test_scope_cannot_be_widened_by_revision_or_provider(self):
        old=self.revise();self.store.drain(self.index)
        foreign={**SCOPE,'tenant':'other-team'}
        with self.assertRaises(RevisionConflict):self.revise(scope=foreign,expected_revision=old['revision_id'])
        with self.assertRaises(PermissionError):self.search(scope=foreign)
        with self.assertRaises(ValueError):self.revise(scope={'service':'orders'})

    def test_wrong_remote_content_hash_is_never_delivered(self):
        self.revise()
        class Bad(IndexFixture):
            def put_revision(self,record):return {**super().put_revision(record),'content_sha256':'a'*64}
        result=self.store.drain(Bad())
        self.assertEqual(result[0]['status'],'retry')
        self.assertEqual(self.store.pending(),1)
        self.assertEqual(self.store.outbox()[0]['last_error'],'ValueError')

    def test_provider_failure_retry_survives_reopen_without_erasing_fact(self):
        old=self.revise()
        class Down:
            def put_revision(self,record):raise OSError('unavailable')
        self.assertEqual(self.store.drain(Down())[0]['status'],'retry')
        reopened=KnowledgeStore(self.store.path,clock=lambda:self.now[0])
        self.assertEqual(reopened.drain(self.index),[])
        self.now[0]+=3
        self.assertEqual(reopened.drain(self.index)[0]['status'],'delivered')
        self.assertEqual(reopened.get(old['revision_id']),old)
        self.index.receipts[old['revision_id']]['uri']='fixture://forged'
        with self.assertRaises(ValueError):self.search()

    def test_concurrent_process_revision_has_exactly_one_cas_winner(self):
        old=self.revise()
        # A fresh interpreter avoids inheriting the suite's HTTP/client threads.
        context=multiprocessing.get_context('spawn')
        gate=context.Event();output=context.Queue()
        children=[context.Process(target=revise_process,args=(self.store.path,old['revision_id'],str(i),gate,output)) for i in range(4)]
        for child in children:child.start()
        gate.set()
        try:
            results=[output.get(timeout=10) for _ in children]
            for child in children:
                child.join(10);self.assertEqual(child.exitcode,0)
        finally:
            for child in children:
                if child.is_alive():child.terminate()
                child.join(10)
            output.close();output.join_thread()
        self.assertEqual(sum(x[0]=='success' for x in results),1)
        head=self.store.head(SCOPE,'diagnostic-route')
        self.assertEqual(head['parent_revision'],old['revision_id'])

    def test_crash_after_remote_effect_reuses_same_idempotent_revision(self):
        old=self.revise()
        script='''import os,json,sys
from pathlib import Path
from self_evolving_harness.knowledge import KnowledgeStore,content_digest
class Remote:
 def put_revision(self,record):
  Path(sys.argv[2]).write_text(json.dumps(record))
  os._exit(42)
KnowledgeStore(sys.argv[1],clock=lambda:100).drain(Remote(),lease_seconds=1)
'''
        effect=self.root/'remote-effect.json'
        env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.store.path),str(effect)],env=env,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,42)
        self.assertEqual(json.loads(effect.read_text()),old)
        self.assertEqual(self.store.outbox()[0]['status'],'sending')
        self.assertEqual(self.store.drain(self.index),[])
        self.now[0]=102
        remote=self.index
        original=remote.put_revision
        def reconcile(record):
            self.assertEqual(record,json.loads(effect.read_text()))
            return original(record)
        remote.put_revision=reconcile
        self.assertEqual(self.store.drain(remote)[0]['status'],'delivered')
        self.assertEqual(self.store.outbox()[0]['attempts'],2)

    def test_expired_owner_cannot_commit_after_new_owner_delivery(self):
        old=self.revise()
        entered=threading.Event();release=threading.Event();errors=[]
        index=self.index
        class Slow:
            def put_revision(inner,record):
                entered.set();release.wait(5)
                return index.put_revision(record)
        def run():
            try:self.store.drain(Slow(),owner='stale',lease_seconds=1)
            except RuntimeError as error:errors.append(str(error))
        thread=threading.Thread(target=run);thread.start()
        self.assertTrue(entered.wait(3));self.now[0]=102
        self.store.drain(index,owner='current')
        release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors),1)
        self.assertEqual(self.store.outbox()[0]['owner'],'current')
        self.assertEqual(self.store.outbox()[0]['status'],'delivered')
        self.assertEqual(self.search()['facts'][0]['revision'],old)

    def test_sources_require_real_reference_shape_and_are_detached_from_caller(self):
        with self.assertRaises(ValueError):self.revise(sources=[{'summary':'a model said so'}])
        sources=[{**SOURCES[0],'evidence_path':'sessions/original.json'}]
        content={'nested':{'value':'actual'}}
        record=self.revise(sources=sources,content=content)
        content['nested']['value']='rewritten';sources[0]['item_id']=123
        self.assertEqual(self.store.get(record['revision_id'])['sources'][0]['item_id'],17)
        self.assertEqual(self.store.get(record['revision_id'])['content']['nested']['value'],'actual')


if __name__=='__main__':unittest.main()

"""Provider contract tests; optional real-server checks are explicitly opt-in."""
import json,os
from hashlib import sha256
from pathlib import Path
import runpy,sys,tempfile,time,unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'src'))
from self_evolving_harness.adapters.openviking import OpenVikingProvider,scope_key
from self_evolving_harness.knowledge import KnowledgeStore
from self_evolving_harness.learning import canonical

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.scope={'tenant':'tenant','environment':'environment','service':'payments'}
        self.p=OpenVikingProvider('http://127.0.0.1:1933','test')
    def test_scope_and_endpoint_reject_unbound_input(self):
        for scope in ({}, {**self.scope,'other':'scope'}):
            with self.assertRaises(ValueError):scope_key(scope)
        for endpoint in ['http://external.example','https://user:password@example.com','https://example.com/?token=x']:
            with self.assertRaises(ValueError):OpenVikingProvider(endpoint,'test')
    def test_immutable_write_readback_must_match_all_bytes(self):
        record={'revision_id':'a'*64,'scope':self.scope,'content':{'note':'original'}}
        def http(method,path,payload=None,params=None):
            if path.endswith('/mkdir'):return {}
            if path.endswith('/batch-write'):return {'result':{'root_uri':self.p.root(self.scope),'created':[payload['operations'][0]['uri']],'updated':[],'queue_status':{'Semantic':{'processed':1,'error_count':0},'Embedding':{'processed':1,'error_count':0}}}}
            return {'result':'tampered'}
        with patch.object(self.p,'_request',side_effect=http),self.assertRaisesRegex(ValueError,'mismatch'):
            self.p.put_revision(record)
    def test_failed_index_keeps_publication_pending_and_unchanged_retry_confirms_refresh(self):
        record={'revision_id':'a'*64,'scope':self.scope,'content':{'note':'original'}}
        uri=self.p.root(self.scope)+'/rev-'+'a'*64+'.md';attempt=[0]
        def http(method,path,payload=None,params=None):
            if path.endswith('/mkdir'):return {}
            if path.endswith('/batch-write'):
                attempt[0]+=1
                if attempt[0]==1:raise HTTPError('http://loopback/batch-write',500,'REFRESH_FAILED',{},None)
                return {'result':{'root_uri':self.p.root(self.scope),'created':[],'unchanged':[uri],'updated':[],
                    'queue_status':{'Semantic':{'processed':1,'error_count':0},'Embedding':{'processed':1,'error_count':0}}}}
            return {'result':canonical(record)}
        receipt={'uri':uri,'revision_id':record['revision_id'],'content_sha256':sha256(canonical(record).encode()).hexdigest()}
        with patch.object(self.p,'_request',side_effect=http),patch.object(self.p,'_search',return_value=[receipt]) as visibility:
            with self.assertRaises(HTTPError):self.p.put_revision(record)
            result=self.p.put_revision(record)
        self.assertEqual(result['revision_id'],record['revision_id']);self.assertEqual(attempt[0],2)
        self.assertEqual(visibility.call_args.kwargs['target_uri'],uri)
    def test_raw_storage_and_successful_queues_without_native_visibility_are_not_delivery(self):
        record={'revision_id':'a'*64,'scope':self.scope,'content':{'note':'recovery'}}
        uri=self.p.root(self.scope)+'/rev-'+'a'*64+'.md'
        def http(method,path,payload=None,params=None):
            if path.endswith('/mkdir'):return {}
            if path.endswith('/batch-write'):return {'result':{'root_uri':self.p.root(self.scope),'created':[uri],'updated':[],
                'queue_status':{'Semantic':{'processed':1,'error_count':0},'Embedding':{'processed':0,'error_count':0}}}}
            return {'result':canonical(record)}
        with patch.object(self.p,'_request',side_effect=http),patch.object(self.p,'_search',return_value=[]),self.assertRaisesRegex(RuntimeError,'searchable'):
            self.p.put_revision(record)
    def test_conflict_and_missing_or_failed_index_receipts_never_become_delivery(self):
        record={'revision_id':'a'*64,'scope':self.scope,'content':{}}
        uri=self.p.root(self.scope)+'/rev-'+'a'*64+'.md'
        def http(method,path,payload=None,params=None):
            if path.endswith('/mkdir'):return {}
            if path.endswith('/batch-write'):raise HTTPError('http://loopback/batch-write',409,'conflict',{},None)
            self.fail('Conflict must not be rescued by raw readback')
        with patch.object(self.p,'_request',side_effect=http),self.assertRaises(HTTPError):self.p.put_revision(record)
        for queues in ({},None,{'Semantic':{'processed':0,'error_count':1},'Embedding':{'processed':0,'error_count':0}}):
            def invalid(method,path,payload=None,params=None):
                if path.endswith('/mkdir'):return {}
                if path.endswith('/batch-write'):
                    return {'result':{'root_uri':self.p.root(self.scope),'created':[uri],'updated':[],'queue_status':queues}}
                self.fail('Invalid index attestation must not reach readback')
            with patch.object(self.p,'_request',side_effect=invalid),self.assertRaises(RuntimeError):self.p.put_revision(record)
    def test_native_search_never_reads_a_foreign_scope_uri(self):
        calls=[]
        class Native:
            def handle_tool_call(_,name,args):
                calls.append(name);return json.dumps({'results':[{'type':'resource','uri':'viking://resources/foreign/rev-'+'a'*64+'.md'}]})
        with patch.object(self.p,'_native',return_value=Native()):
            self.assertEqual(self.p.search(self.scope,'payments'),[])
        self.assertEqual(calls,['viking_search'])
    def test_search_deadline_covers_followup_reads(self):
        self.p.retrieval_timeout=.01;uri=self.p.root(self.scope)+'/rev-'+'a'*64+'.md'
        class Native:
            def handle_tool_call(_,name,args):
                time.sleep(.02);return json.dumps({'results':[{'type':'resource','uri':uri}]})
        with patch.object(self.p,'_native',return_value=Native()),self.assertRaises(TimeoutError):
            self.p.search(self.scope,'payments')
    def test_local_encoder_is_nonconstant_normalized_and_lexical(self):
        encode=runpy.run_path(str(ROOT/'deploy/openviking.py'))['encode']
        a,b,c=map(encode,['payments repeated 503','payments 503 recovery','inventory stock healthy'])
        self.assertNotEqual(a,c);self.assertAlmostEqual(sum(x*x for x in a),1)
        self.assertGreater(sum(x*y for x,y in zip(a,b)),sum(x*y for x,y in zip(a,c)))

@unittest.skipUnless(os.environ.get('RSI_NATIVE_OPENVIKING_ENDPOINT'),'opt-in real OpenViking 0.4.12 with bounded indexing calls')
class NativeProviderTests(unittest.TestCase):
    def test_real_native_search_revision_supersession_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store=KnowledgeStore(Path(directory)/'knowledge.sqlite3')
            p=OpenVikingProvider(os.environ['RSI_NATIVE_OPENVIKING_ENDPOINT'],'acceptance-'+uuid4().hex,retrieval_timeout=15)
            scope={'tenant':'native-test','environment':'isolated-world','service':'payments'}
            sources=[{'session_id':'constructed-contract-fixture','item_id':1,'sha256':'a'*64}]
            old=store.revise(scope=scope,key='recovery',content={'note':'payments 503 require two healthy windows'},sources=sources)
            store.drain(p)
            found=store.search(p,scope=scope,query='payments 503 healthy windows')
            self.assertTrue(found["facts"])
            new=store.revise(scope=scope,key='recovery',content={'note':'payments recovery now requires three healthy windows'},sources=sources,expected_revision=old['revision_id'])
            store.drain(p)
            found=store.search(p,scope=scope,query='payments recovery healthy windows')
            self.assertEqual({r['revision']['revision_id'] for r in found['facts']},{new['revision_id']})
            self.assertEqual(p.search({**scope,'tenant':'foreign-tenant'},'payments recovery'),[])

if __name__=='__main__':unittest.main()

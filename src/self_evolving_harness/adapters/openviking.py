"""Pinned OpenViking storage and native Hermes read-only search adapter.

Local KnowledgeStore owns scope, current heads, expiry and revocation. External
indexes are candidate discovery, never the authority for a current fact.
"""
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import math
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from ..learning import canonical

HERMES_REVISION = 'aaf9688519cca58dd5f76a589a0911aff269b060'


def scope_key(scope):
    if not isinstance(scope, dict) or set(scope) != {'tenant','environment','service'} or any(
            not isinstance(x,str) or not x.strip() for x in scope.values()):
        raise ValueError('Memory requires exact tenant/environment/service scope')
    return sha256(canonical(scope).encode()).hexdigest()


class OpenVikingProvider:
    def __init__(self, endpoint, namespace, *, api_key='', account='default', user='default',
                 actor='rsi-memory', hermes_source=None, timeout=100, retrieval_timeout=5):
        parsed=urlsplit(endpoint)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('','/') or not (parsed.scheme=='https' or parsed.scheme=='http'
                and parsed.hostname in {'127.0.0.1','localhost','::1'})):
            raise ValueError('Use an HTTPS or loopback HTTP OpenViking root without embedded credentials')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',namespace):
            raise ValueError('Use a stable deployment namespace')
        self.endpoint,self.namespace=endpoint.rstrip('/'),namespace
        self.api_key,self.account,self.user,self.actor=api_key,account,user,actor
        if type(retrieval_timeout) not in (int,float) or not math.isfinite(retrieval_timeout) or retrieval_timeout<=0:
            raise ValueError('Use a finite positive retrieval timeout')
        self.retrieval_timeout=retrieval_timeout
        self.timeout=timeout
        self.hermes_source=hermes_source

    def root(self,scope):
        return f'viking://resources/rsi-{self.namespace}/scope-{scope_key(scope)}'

    def _request(self,method,path,payload=None,params=None):
        headers={'Content-Type':'application/json','X-OpenViking-Account':self.account,
                 'X-OpenViking-User':self.user,'X-OpenViking-Actor-Peer':self.actor}
        if self.api_key:headers.update({'X-API-Key':self.api_key,'Authorization':'Bearer '+self.api_key})
        request=Request(self.endpoint+path+('?' + urlencode(params) if params else ''),
                        data=canonical(payload).encode() if payload is not None else None,
                        headers=headers,method=method)
        with build_opener(ProxyHandler({})).open(request,timeout=self.timeout) as response:
            return json.load(response)

    def health(self):
        result=self._request('GET','/health')
        return result.get('status') in ('ok','healthy','running')

    def put_revision(self,record):
        identity=record['revision_id']
        if not re.fullmatch(r'[0-9a-f]{64}',identity):raise ValueError('Invalid revision identity')
        root=self.root(record['scope']);uri=f'{root}/rev-{identity}.md'
        body=canonical(record)
        for directory in (f'viking://resources/rsi-{self.namespace}',root):
            try:self._request('POST','/api/v1/fs/mkdir',{'uri':directory})
            except HTTPError as error:
                if error.code!=409:raise
        # Pinned 0.4.12 retries matching bytes as `unchanged` AND refreshes
        # their index again while wait=True. HTTP 409 is an actual precondition
        # conflict; raw readback alone must never manufacture an indexed receipt.
        result=self._request('POST','/api/v1/content/batch-write',{'root_uri':root,'operations':[
            {'uri':uri,'content':body,'precondition':{'kind':'create_if_absent'}}],
            'wait':True,'timeout':90})['result']
        queues=result.get('queue_status')
        if (result.get('root_uri')!=root or result.get('updated')
                or uri not in result.get('created',[])+result.get('unchanged',[])
                or not isinstance(queues,dict)):
            raise RuntimeError('OpenViking did not attest immutable indexed publication')
        for name in ('Semantic','Embedding'):
            status=queues.get(name)
            if (not isinstance(status,dict) or type(status.get('error_count')) is not int
                    or status['error_count']!=0 or status.get('errors')
                    or type(status.get('processed')) is not int or status['processed']<0):
                raise RuntimeError('OpenViking failed indexed publication: '+name)
        saved=self._request('GET','/api/v1/content/read',params={'uri':uri,'raw':True})['result']
        if saved!=body:raise ValueError('OpenViking immutable revision content mismatch')
        receipt={'uri':uri,'revision_id':identity,'content_sha256':sha256(body.encode()).hexdigest()}
        # 0.4.12 can report zero queue errors before an asynchronous vector
        # failure reaches its tracker. Only native search + full read visibility
        # proves the revision usable; raw storage is insufficient. Search the
        # immutable file itself so unrelated historical revisions cannot crowd it.
        query=' '.join((record['scope']['service'],str(record.get('key','')),canonical(record['content'])))[:4000]
        if receipt not in self._search(record['scope'],query,1,target_uri=uri):
            raise RuntimeError('OpenViking indexed revision is not yet natively searchable')
        return receipt

    def _native(self,deadline):
        source=Path(self.hermes_source or os.environ.get('HERMES_SRC') or
                    Path(__file__).resolve().parents[3]/'.deps/hermes-agent').resolve()
        actual=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
        if actual!=HERMES_REVISION:raise ValueError('Native OpenViking adapter requires pinned Hermes')
        if str(source) not in sys.path:sys.path.insert(0,str(source))
        native=importlib.import_module('plugins.memory.openviking')
        provider=native.OpenVikingMemoryProvider()
        # Pinned provider explicitly supports caller-wired clients before
        # initialize(). This avoids global env mutation, session recovery and
        # automatic commits; only native search/read tools are invoked.
        class DeadlineClient(native._VikingClient):
            def bounded(self,kwargs):
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('OpenViking retrieval deadline exceeded')
                kwargs['timeout']=min(remaining,kwargs.get('timeout',remaining))
                return kwargs
            def get(self,path,**kwargs):return super().get(path,**self.bounded(kwargs))
            def post(self,path,payload=None,**kwargs):return super().post(path,payload,**self.bounded(kwargs))
        provider._client=DeadlineClient(self.endpoint,self.api_key,
                    account=self.account,user=self.user,agent=self.actor)
        return provider

    def search(self,scope,query,limit=10):
        return self._search(scope,query,limit)

    def _search(self,scope,query,limit,target_uri=None):
        if not isinstance(query,str) or not query.strip() or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('Use a nonempty query and bounded result count')
        root=self.root(scope);deadline=time.monotonic()+self.retrieval_timeout;provider=self._native(deadline)
        def call(name,args):
            if time.monotonic()>=deadline:raise TimeoutError('OpenViking retrieval deadline exceeded')
            value=json.loads(provider.handle_tool_call(name,args))
            if not isinstance(value,dict) or 'error' in value:raise RuntimeError('Native OpenViking tool failed: '+name)
            return value
        hits=call('viking_search',{'query':query,'scope':target_uri or root,'mode':'fast','limit':limit})
        results=[]
        for hit in hits.get('results',[]):
            uri=hit.get('uri','')
            if hit.get('type')!='resource' or not re.fullmatch(re.escape(root)+r'/rev-[0-9a-f]{64}\.md',uri):continue
            read=call('viking_read',{'uri':uri,'level':'full'})
            if read.get('uri')!=uri or read.get('resolved_uri')!=uri or read.get('level')!='full':
                raise ValueError('Native memory read citation mismatch')
            content=read['content'];record=json.loads(content)
            if record.get('scope')!=scope or uri!=f"{root}/rev-{record['revision_id']}.md" or content!=canonical(record):
                raise ValueError('Retrieved revision outside requested scope or immutable identity')
            results.append({'uri':uri,'revision_id':record['revision_id'],
                            'content_sha256':sha256(content.encode()).hexdigest()})
        return results[:limit]

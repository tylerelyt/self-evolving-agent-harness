"""Run a real pinned OpenViking server with bounded indexing and local encoding.

The default encoder is deterministic character n-gram hashing, not a semantic
embedding model. Retrieval integration is testable without buying embeddings;
semantic ranking quality must be evaluated separately with a production encoder.
"""
import argparse
from hashlib import blake2b
from http.server import BaseHTTPRequestHandler
import json,math,os
from pathlib import Path
import shlex,signal,sys,threading,time,hmac,secrets
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from self_evolving_harness.http_server import LoopbackHTTPServer


def encode(text,dimension=512):
    text=' '.join(text.lower().split());vector=[0.0]*dimension
    for n in (1,2,3):
        for i in range(max(1,len(text)-n+1)):
            token=text[i:i+n].encode();number=int.from_bytes(blake2b(token,digest_size=8).digest(),'big')
            vector[number%dimension]+=1 if number&(1<<63) else -1
    length=math.sqrt(sum(x*x for x in vector))
    return [x/length if length else 0.0 for x in vector]


def write(path,data):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2)+'\n');temp.replace(path)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True);parser.add_argument('--port',type=int,default=1933)
    parser.add_argument('--model-env-file',type=Path);parser.add_argument('--max-model-requests',type=int,default=30)
    args=parser.parse_args();state=args.state.resolve();state.mkdir(parents=True,exist_ok=True,mode=0o700)
    import fcntl
    lock=(state/'.server.lock').open('a+')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:parser.error('This OpenViking workspace already has an owner')
    from importlib.metadata import version
    if version('openviking')!='0.4.12':parser.error('Use the pinned OpenViking 0.4.12 server environment')
    values={k:os.environ.get(k,'') for k in ('DEEPSEEK_API_KEY','DEEPSEEK_MODEL','DEEPSEEK_BASE_URL')}
    if args.model_env_file:
        for line in args.model_env_file.read_text().splitlines():
            key,sep,value=line.strip().removeprefix('export ').partition('=')
            if sep and key in values and not values[key]:
                parsed=shlex.split(value,comments=True)
                if len(parsed)==1:values[key]=parsed[0]
    if not values['DEEPSEEK_API_KEY']:parser.error('Provide DeepSeek credentials for actual indexing summaries')
    endpoint=values['DEEPSEEK_BASE_URL'] or 'https://api.deepseek.com'
    from urllib.parse import urlsplit
    parsed=urlsplit(endpoint)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use an explicit credential-free HTTPS model endpoint')
    model=values['DEEPSEEK_MODEL'] or 'deepseek-flash'
    ledger_path=state/'model-usage.json'
    ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {'encoder':'local-char-ngram-hashing-v1','semantic_embedding':False,'requests':[],'embedding_batches':0}
    mutex=threading.Lock()
    local_token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,code,body):
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+local_token):
                self.send(401,b'{}');return
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=4*1024*1024:self.send(400,b'{}');return
            payload=json.loads(self.rfile.read(size))
            if self.path.endswith('/embeddings'):
                items=payload.get('input');items=[items] if isinstance(items,str) else items
                if not isinstance(items,list) or not all(isinstance(x,str) for x in items):self.send(400,b'{}');return
                data={'object':'list','model':'local-char-ngram-hashing-v1','data':[{'object':'embedding','index':i,'embedding':encode(text)} for i,text in enumerate(items)],'usage':{'prompt_tokens':0,'total_tokens':0}}
                with mutex:ledger['embedding_batches']+=1;write(ledger_path,ledger)
                self.send(200,json.dumps(data).encode());return
            if not self.path.endswith('/chat/completions'):self.send(404,b'{}');return
            with mutex:
                if len(ledger['requests'])>=args.max_model_requests:self.send(429,b'{"error":{"message":"Indexing request budget exhausted"}}');return
                row={'index':len(ledger['requests'])+1,'status':'requested','started_at':time.time()};ledger['requests'].append(row);write(ledger_path,ledger)
            payload.update(model=model,stream=False,thinking={'type':'disabled'})
            request=Request(endpoint.rstrip('/')+'/chat/completions',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+values['DEEPSEEK_API_KEY']})
            try:
                try:
                    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=90) as response:code,body=response.status,response.read()
                except HTTPError as error:code,body=error.code,error.read()
                result=json.loads(body)
                with mutex:row.update(status='completed' if code==200 else 'failed',http_status=code,usage=result.get('usage'));write(ledger_path,ledger)
                self.send(code,body)
            except Exception as error:
                with mutex:row.update(status='transport_error',error_type=type(error).__name__);write(ledger_path,ledger)
                self.send(502,b'{"error":{"message":"Indexing transport failed"}}')
    gateway=LoopbackHTTPServer(('127.0.0.1',0),Handler);gateway.daemon_threads=True
    thread=threading.Thread(target=gateway.serve_forever,daemon=True);thread.start()
    config={'server':{'host':'127.0.0.1','port':args.port},'storage':{'workspace':str(state/'data'),'vectordb':{'backend':'local'},'agfs':{'backend':'local'}},
            'vlm':{'provider':'openai','model':model,'api_base':f'http://127.0.0.1:{gateway.server_port}/v1','api_key':local_token,'max_tokens':2048,'max_concurrent':1},
            'embedding':{'dense':{'provider':'openai','model':'local-char-ngram-hashing-v1','api_base':f'http://127.0.0.1:{gateway.server_port}/v1','api_key':local_token,'dimension':512,'encoding_format':'float'}},
            'memory':{'extraction_enabled':False,'session_skill_extraction_enabled':False}}
    path=state/'ov.conf';write(path,config);path.chmod(0o600)
    record={'status':'starting','pid':os.getpid(),'endpoint':f'http://127.0.0.1:{args.port}','version':'0.4.12','encoder':ledger['encoder'],'semantic_embedding':False}
    write(state/'server.json',record)
    ready_stop=threading.Event()
    def mark_ready():
        while not ready_stop.wait(.2):
            try:
                with build_opener(ProxyHandler({})).open(record['endpoint']+'/health',timeout=1) as response:
                    if response.status==200:
                        record['status']='ready';write(state/'server.json',record);return
            except Exception:pass
    ready_thread=threading.Thread(target=mark_ready,daemon=True);ready_thread.start()
    os.environ['OPENVIKING_CONFIG_FILE']=str(path);os.environ['NO_PROXY']='127.0.0.1,localhost,::1';os.environ['no_proxy']=os.environ['NO_PROXY']
    sys.argv=['openviking-server','--config',str(path)]
    # The launcher filename must not shadow the installed openviking package.
    sys.path=[p for p in sys.path if Path(p or '.').resolve()!=Path(__file__).resolve().parent]
    from openviking_cli.server_bootstrap import main as serve
    # Uvicorn restores and re-raises captured signals after graceful shutdown.
    # A Python exit handler lets this launcher's finally persist stopped status
    # instead of the default SIGTERM action bypassing cleanup entirely.
    def exit_signal(signum,frame):raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,exit_signal)
    try:serve()
    finally:
        ready_stop.set();ready_thread.join();gateway.shutdown();thread.join();gateway.server_close();record['status']='stopped';write(state/'server.json',record)
if __name__=='__main__':main()

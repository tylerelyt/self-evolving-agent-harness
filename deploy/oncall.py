"""Run maintained On-call with its configured, supervised memory service."""
from contextlib import contextmanager
import json,os
from pathlib import Path
import signal,socket,subprocess,sys,time
from urllib.request import Request,build_opener,ProxyHandler
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from apps.oncall.__main__ import main as application


def read(path):return json.loads(Path(path).read_text())
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n');temporary.replace(path)

def path_setting(workspace,value,default):
    value=Path(value if value is not None else default).expanduser()
    return (workspace/value).resolve() if not value.is_absolute() else value.resolve()

def healthy(endpoint,timeout=2):
    parsed=urlsplit(endpoint)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in ('','/') or not (parsed.scheme=='https' or parsed.scheme=='http'
        and parsed.hostname in {'127.0.0.1','localhost','::1'})):
        raise ValueError('Memory endpoint must be HTTPS or loopback HTTP without credentials')
    try:
        with build_opener(ProxyHandler({})).open(endpoint.rstrip('/')+'/health',timeout=timeout) as response:
            return response.status==200
    except OSError:return False

def stop_process(process):
    if process.poll() is None:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:return
        try:process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()

@contextmanager
def memory_service(settings,workspace):
    """Own only our server; post-start outages are handled by the memory worker."""
    if settings.get('backend')!='openviking':raise ValueError('Dedicated memory requires OpenViking')
    if 'api_key' in settings:raise ValueError('Use api_key_env, not credentials in a deployment profile')
    managed=settings.get('managed',True)
    if type(managed) is not bool:raise ValueError('memory.managed must be a boolean')
    process=None
    with ExitSignals():
        try:
            if managed:
                state=path_setting(workspace,settings.get('server_state_dir'),'openviking')
                state.mkdir(parents=True,exist_ok=True,mode=0o700)
                port=settings.get('port',1933)
                if type(port) is not int or not 0<=port<=65535:raise ValueError('Invalid OpenViking port')
                if port==0:
                    old=state/'server.json'
                    if old.exists():port=urlsplit(read(old)['endpoint']).port
                    if not port:
                        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                endpoint=f'http://127.0.0.1:{port}'
                # Preserve the venv executable entry: resolving its symlink
                # selects base Python and silently loses installed packages.
                python_value=Path(settings.get('server_python',str(ROOT/'var/openviking/venv/bin/python'))).expanduser()
                python=Path(os.path.abspath(workspace/python_value))
                env_file=path_setting(workspace,settings.get('model_env_file'),ROOT/'.env')
                if settings.get('server_command'):
                    command=settings['server_command']
                    if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x for x in command):
                        raise ValueError('memory.server_command must be a trusted argument array')
                    command=[part.replace('{state}',str(state)).replace('{port}',str(port)).replace('{repo}',str(ROOT)) for part in command]
                else:
                    if not python.is_file():raise RuntimeError('OpenViking runtime missing: run deploy/setup_openviking.py before launch')
                    command=[str(python),'-B',str(ROOT/'deploy/openviking.py'),'--state',str(state),'--port',str(port),
                             '--model-env-file',str(env_file),'--max-model-requests',str(settings.get('max_model_requests',60))]
                log=state/'server.log'
                with log.open('a') as output:
                    process=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                timeout=settings.get('startup_timeout',90)
                if type(timeout) not in (int,float) or not 0<timeout<=180:raise ValueError('Invalid memory startup timeout')
                until=time.monotonic()+timeout
                while True:
                    if process.poll() is not None:raise RuntimeError(f'OpenViking exited before ready; inspect {log}')
                    marker=state/'server.json'
                    try:ready=read(marker)
                    except (OSError,ValueError):ready={}
                    if (ready.get('pid')==process.pid and ready.get('status')=='ready'
                            and ready.get('endpoint')==endpoint and healthy(endpoint)):break
                    if time.monotonic()>=until:raise RuntimeError(f'OpenViking did not become ready; inspect {log}')
                    time.sleep(.1)
            else:
                endpoint=settings.get('endpoint','').rstrip('/')
                if not healthy(endpoint):raise RuntimeError('Configured external OpenViking endpoint is unavailable')
            template={key:value for key,value in settings.items() if key in {
                'tenant','namespace','account','user','api_key_env','services','retrieval_timeout','publication_timeout','fact_ttl_seconds'}}
            template.update(schema_version=1,backend='openviking',endpoint=endpoint,
                            state_dir=str(path_setting(workspace,settings.get('state_dir'),'knowledge')))
            path=workspace/'memory-config.json';write(path,template)
            yield path
        finally:
            if process is not None:stop_process(process)

class ExitSignals:
    def __enter__(self):
        self.previous={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
        for sig in self.previous:signal.signal(sig,lambda _sig,_frame:sys.exit(0))
    def __exit__(self,*_args):
        for sig,handler in self.previous.items():signal.signal(sig,handler)


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    settings_path=os.environ.get('RSI_SETTINGS_PATH')
    if '--help' in argv or not argv or argv[0]!='serve' or not settings_path:return application(argv)
    settings=read(settings_path).get('settings',{}).get('memory')
    if settings is None:return application(argv)
    if '--memory-config' in argv:raise ValueError('Use either deployment memory settings or an explicit CLI binding')
    workspace=Path(os.environ['RSI_STATE_DIR']).resolve()
    with memory_service(settings,workspace) as path:return application([*argv,'--memory-config',str(path),'--enable-memory'])

if __name__=='__main__':
    with ExitSignals():main()

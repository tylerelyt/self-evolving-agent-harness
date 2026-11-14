"""Managed-process contract checks; the fake server is not OpenViking acceptance."""
import importlib.util,json,os
from pathlib import Path
import signal,subprocess,sys,tempfile,time,unittest
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('memory_deployment',ROOT/'deploy/oncall.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

SERVER='''import argparse,json,os,signal,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from socketserver import TCPServer
class NumericHTTPServer(HTTPServer):
 def server_bind(self):
  TCPServer.server_bind(self)
  self.server_name,self.server_port=self.server_address[:2]
p=argparse.ArgumentParser();p.add_argument('--state');p.add_argument('--port',type=int);a=p.parse_args();state=Path(a.state)
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
server=NumericHTTPServer(('127.0.0.1',a.port),Handler)
record=state/'server.json';record.write_text(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),'pgrp':os.getpgrp(),'status':'ready','endpoint':f'http://127.0.0.1:{a.port}'}))
(state/'retained-evidence').touch(exist_ok=True)
def stop(*args):threading.Thread(target=server.shutdown).start()
signal.signal(signal.SIGTERM,stop)
try:server.serve_forever(.1)
finally:server.server_close();r=json.loads(record.read_text());r['status']='stopped';record.write_text(json.dumps(r))
'''

class MemoryDeploymentTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.server=self.root/'contract_server.py';self.server.write_text(SERVER)
  self.settings={'backend':'openviking','managed':True,'port':0,'server_command':[sys.executable,str(self.server),'--state','{state}','--port','{port}'],'startup_timeout':5}
 def pid(self):return json.loads((self.root/'openviking/server.json').read_text())['pid']
 def test_ready_before_binding_restart_keeps_endpoint_and_state_and_cleanup(self):
  with launcher.memory_service(self.settings,self.root) as binding:
   config=json.loads(binding.read_text());pid=self.pid();self.assertTrue(launcher.healthy(config['endpoint']))
   self.assertEqual(config['backend'],'openviking');self.assertEqual(config['state_dir'],str(self.root/'knowledge'))
  self.assertEqual(json.loads((self.root/'openviking/server.json').read_text())['status'],'stopped')
  with self.assertRaises(ProcessLookupError):os.kill(pid,0)
  with launcher.memory_service(self.settings,self.root) as binding:
   self.assertEqual(json.loads(binding.read_text())['endpoint'],config['endpoint'])
   self.assertNotEqual(self.pid(),pid);self.assertTrue((self.root/'openviking/retained-evidence').exists())
 def test_late_server_failure_does_not_kill_the_oncall_owner(self):
  with launcher.memory_service(self.settings,self.root) as binding:
   os.kill(self.pid(),signal.SIGTERM)
   for _ in range(30):
    if not launcher.healthy(json.loads(binding.read_text())['endpoint'],timeout=.1):break
    time.sleep(.1)
   self.assertTrue(binding.exists()) # owner remains in its execution context
 def test_terminating_launcher_cleans_up_its_owned_server(self):
  driver=self.root/'owner.py'
  driver.write_text('import importlib.util,json,time\nfrom pathlib import Path\n' +
      f's=importlib.util.spec_from_file_location("launch",{str(ROOT/"deploy/oncall.py")!r})\n' +
      'm=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n' +
      f'with m.memory_service(json.loads({json.dumps(self.settings)!r}),Path({str(self.root)!r})) as binding:\n' +
      ' print("READY",flush=True)\n while True:time.sleep(.1)\n')
  owner=subprocess.Popen([sys.executable,str(driver)],stdout=subprocess.PIPE,text=True)
  try:
   self.assertEqual(owner.stdout.readline().strip(),'READY');pid=self.pid()
   owner.terminate();owner.wait(timeout=5)
   self.assertEqual(owner.returncode,0)
   with self.assertRaises(ProcessLookupError):os.kill(pid,0)
  finally:
   if owner.poll() is None:owner.kill();owner.wait()
   owner.stdout.close()
 def test_startup_failure_has_no_silent_memory_fallback(self):
  settings={**self.settings,'server_command':[sys.executable,'-c','raise SystemExit(3)']}
  with self.assertRaisesRegex(RuntimeError,'exited before ready'):
   with launcher.memory_service(settings,self.root):self.fail('failed server was accepted')
 def test_default_command_preserves_the_virtualenv_python_entry(self):
  from unittest.mock import patch
  venv=self.root/'real-venv'
  subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True,capture_output=True,timeout=30)
  python=venv/'bin/python'
  self.assertTrue(python.is_symlink())
  checkout=self.root/'checkout';(checkout/'deploy').mkdir(parents=True)
  code=SERVER.replace('import argparse,json,os,signal,threading','import argparse,json,os,signal,threading,sys').replace('a=p.parse_args()','a,_=p.parse_known_args()').replace("'pid':os.getpid()","'pid':os.getpid(),'prefix':sys.prefix,'base_prefix':sys.base_prefix,'executable':sys.executable")
  (checkout/'deploy/openviking.py').write_text(code)
  settings={'backend':'openviking','managed':True,'port':0,'server_python':str(python),'startup_timeout':5}
  popen=launcher.subprocess.Popen;children=[]
  def capture(*args,**kwargs):
   child=popen(*args,**kwargs);children.append(child);return child
  try:
   with patch.object(launcher,'ROOT',checkout), patch.object(launcher.subprocess,'Popen',capture), launcher.memory_service(settings,self.root):
    record=json.loads((self.root/'openviking/server.json').read_text())
    self.assertEqual(record['prefix'],str(venv))
    self.assertNotEqual(record['prefix'],record['base_prefix'])
    self.assertEqual(record['executable'],str(python))
  except RuntimeError:
   marker=self.root/'openviking/server.json'
   record=json.loads(marker.read_text()) if marker.exists() else {}
   reported=record.get('pid');group=None
   if reported:
    try:group=os.getpgid(reported)
    except ProcessLookupError:pass
   raise AssertionError(json.dumps({'status':record.get('status'),'owned_pids':[c.pid for c in children],
       'reported_pid':reported,'reported_group':record.get('pgrp',group),'reported_parent':record.get('ppid'),'exit_codes':[c.poll() for c in children]})) from None

 def test_missing_runtime_reports_install_command(self):
  settings={'backend':'openviking','managed':True,'server_python':str(self.root/'missing')}
  with self.assertRaisesRegex(RuntimeError,'setup_openviking.py'):
   with launcher.memory_service(settings,self.root):self.fail('missing runtime was accepted')

if __name__=='__main__':unittest.main()

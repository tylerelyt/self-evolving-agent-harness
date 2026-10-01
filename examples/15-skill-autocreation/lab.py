#!/usr/bin/env python3
"""启动、环境准备和发请求分开；本文件不调用模型或写长期记忆。"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from common import HERE, BASE, ACTIVE, runtime, observations, save, record, api, now, scrub

_owned_process = None
_owned_runtime = None


def up():
    global _owned_process, _owned_runtime
    if ACTIVE.exists():
        try:
            status = api('/status')
        except (OSError, RuntimeError):
            status = None
        if status:
            raise ValueError('已有本讲服务在线，请先 down；不会覆盖它的入口')
    if not os.environ.get('GLM_API_KEY'):
        raise ValueError('需要 GLM_API_KEY；无凭据回放请设置 LAB_VALIDATION_RUN_ID 后运行 demo')
    run = BASE / 'runs' / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
    run.mkdir(parents=True)
    save(ACTIVE, {'run': str(run.relative_to(BASE))})
    os.environ['SKILL_LAB_RUNTIME'] = str(run)
    token = run / 'http.token'; token.write_text(secrets.token_hex(24)); token.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    obs = observations(); obs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy(); env.pop('BIGMODEL_API_KEY', None)
    with (obs / 'server.log').open('w') as log:
        python = os.environ.get('HERMES_PYTHON', str(HERE.parents[1] / '.deps/hermes-agent/.venv/bin/python'))
        env['PYTHONPATH'] = str(Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent'))) + os.pathsep + env.get('PYTHONPATH', '')
        process = subprocess.Popen([python, '-B', str(HERE / 'oncall_server.py'),
                                    '--port', str(port), '--runtime', str(run)],
                                   cwd=HERE, env=env, stdout=log, stderr=log, start_new_session=True)
    _owned_process, _owned_runtime = process, run
    save(run / 'processes.json', {'oncall': {'pid': process.pid, 'port': port}})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('服务启动失败，详情见 ' + str(obs / 'server.log'))
        try:
            status = api('/status'); print('服务已在线：', scrub(status)); return status
        except (OSError, RuntimeError):
            time.sleep(.25)
    process.terminate(); process.wait(timeout=10)
    raise TimeoutError('健康检查超时，已停止本次进程')



def cleanup_owned(grace=30):
    """只回收本进程启动的服务组；绝不根据 active.json 猜测并杀进程。"""
    global _owned_process, _owned_runtime
    process, run = _owned_process, _owned_runtime
    if process is None:
        return {'owned': False}
    os.environ['SKILL_LAB_RUNTIME'] = str(run)
    forced = False
    try:
        deadline = time.monotonic() + grace
        if process.poll() is None:
            try:
                api('/drain', {}, timeout=1)
            except (OSError, RuntimeError):
                pass
        while process.poll() is None and time.monotonic() < deadline:
            try:
                api('/shutdown', {}, timeout=1)
            except (OSError, RuntimeError):
                pass
            try:
                process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            # 服务以独立进程组启动，学习子进程也在该组内。
            import signal
            forced = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        result = {
            'owned': True, 'pid': process.pid, 'exited': process.poll() is not None,
            'forced': forced, 'unfinished_result': 'unknown' if forced else None,
        }
        record(observations()/'cleanup.json', result)
        return result
    finally:
        if process.poll() is not None:
            _owned_process = None
            _owned_runtime = None


def setup():
    work=runtime()/'business';work.mkdir(exist_ok=False)
    common={'merchant_id':'merchant-demo','amount':6800,'amount_unit':'fen','currency':'CNY'}
    rows=[]
    for order,final,status in [('order-timeout','paid','timeout'),('order-signature','paid','signature_failed'),('order-processing','processing','not_sent')]:
        rows.append({'order':order,'constructed':True,
          'local':{**common,'channel_transaction_id':'channel-'+order,'request_result':'success','order_status':'paying','payment_mode':'single-full'},
          'channel':{**common,'channel_transaction_id':'channel-'+order,'final_status':final},
          'platform':{'payment_status':'paid' if final=='paid' else 'pending','order_status':'paying'},
          'notification':{'notification_status':status,'merchant_processing':'unknown'}})
    save(work/'records.json',rows);(work/'audit.jsonl').touch()
    save(runtime()/'business.json',{'directory':str(work),'constructed':True})
    record(observations()/'setup.json',{'constructed':True,'orders':3,'read_only':True,'business_sha256':__import__('hashlib').sha256((work/'records.json').read_bytes()).hexdigest()})
    print('构造现场已就绪：三笔单笔足额订单，全部查询只读。');return work

def send(sid,message):
    task=api('/tasks',{'session':sid,'message':message})
    print('前台请求已提交：',task['id']);return task['id']

def wait_task(tid,timeout=600):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        task=api('/tasks/'+tid)
        if task['status'] not in ('queued','running'):
            if task['status']!='completed':raise RuntimeError('前台未完成，记录已保存')
            if 'learning_at_return' not in task:
                time.sleep(.05)
                continue
            print(task['result']['final_response']);return task
        time.sleep(.25)
    raise TimeoutError('前台等待超时，不能推定请求未执行')

def wait_review(tid,timeout=660):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        jobs=api('/reviews?after_task='+tid)
        if jobs and all(j['status'] not in ('queued','running') for j in jobs):
            if any(j['status']!='completed' for j in jobs):raise RuntimeError('后台学习失败，记录保留，未采用候选')
            return jobs
        time.sleep(.5)
    raise TimeoutError('后台等待超时，未采用候选')

def down(timeout=600):
    # 长时间等待期间不随 active.json 切换到另一轮服务。
    os.environ['SKILL_LAB_RUNTIME'] = str(runtime())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = api('/shutdown', {})
            meta = json.loads((runtime() / 'processes.json').read_text())['oncall']
            for _ in range(100):
                with socket.socket() as sock:
                    if sock.connect_ex(('127.0.0.1', meta['port'])) != 0:
                        record(observations() / 'shutdown.json', {**result, **meta, 'port_closed': True})
                        print('本次服务已停止，端口已释放；日志和临时现场保留。'); return
                time.sleep(.1)
            raise TimeoutError('停止已受理，但端口尚未释放')
        except RuntimeError as e:
            if '仍有前台或后台任务' not in str(e): raise
        time.sleep(.5)
    raise TimeoutError('后台尚未结束，未强制中断服务；请稍后 down')



def main():
    p=argparse.ArgumentParser(description='第15讲常驻服务管理');p.add_argument('action',choices=['up','setup','down','status'])
    a=p.parse_args()
    if a.action=='up':
        from credentials import load_credentials
        load_credentials();up()
    elif a.action=='setup':setup()
    elif a.action=='down':down()
    else:print(api('/status'))
if __name__=='__main__':main()

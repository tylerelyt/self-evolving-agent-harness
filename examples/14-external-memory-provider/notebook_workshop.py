"""复用公共 GLM 本地适配器，分幕操作真实 Hermes 服务。"""
import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
from common import record, HERE


class NotebookWorkshop:
    def __init__(self):
        self.adapter = None
        self.worker = None
        self.closed = False
        atexit.register(self.close)

    def prepare(self):
        env = os.environ.copy()
        env.pop('BIGMODEL_API_KEY', None)
        env.pop('SESSION_LAB_RUNTIME', None)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        if not env.get('LAB_VALIDATION_RUN_ID'):
            from credentials import load_credentials
            load_credentials()
            # 原样使用父代理的 GLM_BASE_URL；不另起可能绕过收费拦截的代理。
        else:
            env.pop('GLM_API_KEY', None)
        src = HERE.parents[1] / '.deps/hermes-agent'
        env['PYTHONPATH'] = str(src) + os.pathsep + env.get('PYTHONPATH', '')
        self.worker = subprocess.Popen([str(src / '.venv/bin/python'), '-B', '-u', str(HERE / 'notebook_worker.py')],
                                       cwd=HERE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
        return self.step('prepare')

    def step(self, action):
        self.worker.stdin.write(json.dumps({'action': action}) + '\n')
        self.worker.stdin.flush()
        line = self.worker.stdout.readline()
        if not line:
            raise RuntimeError('分幕执行器退出：' + self.worker.stderr.read())
        response = json.loads(line)
        print(response['output'], end='')
        if not response['ok']:
            raise RuntimeError(response['error'])
        result = response['result']
        if action == 'prepare':
            print('提供者读写回执、内置记忆对照与报告将在本次 observations 目录生成：')
            print(result['observations'])
        if action == 'finish':
            self.worker.wait(timeout=10)
            mode = 'offline' if os.environ.get('LAB_VALIDATION_RUN_ID') else 'live'
            if self.adapter:
                requests = len(self.adapter.requests)
                port = self.adapter.observer.server_port
                self.adapter.close()
                result.update(adapter_requests=requests, adapter_port=port, adapter_closed=True,
                              adapter_runtime=str(self.adapter.runtime.relative_to(HERE)))
                record(HERE / 'validation' / (mode + '-latest.json'), result)
                record(HERE / result['observation_directory'] / 'result.json', result)
                print('公共 GLM 本地适配器：', requests, '次请求；连接和进程已关闭。')
            self.close()
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.worker and self.worker.poll() is None:
            self.worker.stdin.close()
            try:
                self.worker.wait(timeout=40)
            except subprocess.TimeoutExpired:
                self.worker.terminate()
                try:
                    self.worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.worker.kill()
                    self.worker.wait(timeout=5)
        if self.worker:
            for pipe in (self.worker.stdin, self.worker.stdout, self.worker.stderr):
                pipe.close()
        if self.adapter:
            self.adapter.close()
        atexit.unregister(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

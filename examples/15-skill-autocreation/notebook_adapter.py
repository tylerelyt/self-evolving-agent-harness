"""沿用公共 Lab；仅在本讲生成配置中适配 Hermes 的 Chat Completions。"""
import os
import signal
import subprocess
import threading
import time
import urllib.request
from notebook_support import Lab


class HermesNotebookAdapter(Lab):
    def _start(self, key):
        super()._start(key)
        # 公共模板为 Responses 选用 chat_completions/ 前缀；Hermes 直接发
        # Chat Completions，该前缀会被误传给供应商。只改本次生成配置。
        config = self.runtime / 'litellm.yaml'
        config.write_text(config.read_text().replace('openai/chat_completions/glm-5.2', 'openai/glm-5.2'))
        args = self.proxy.args
        port = int(args[args.index('--port') + 1])
        os.killpg(self.proxy.pid, signal.SIGTERM)
        try:
            self.proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # 此时尚未提交教学任务，只停止本次专属适配器进程组。
            os.killpg(self.proxy.pid, signal.SIGKILL)
            self.proxy.wait(timeout=5)
        self._log_thread.join(timeout=10)
        self.proxy.stdout.close()
        env = os.environ.copy()
        env.update(GLM_API_KEY=key, LAB_PROXY_TOKEN=self._secrets[1],
                   PYTHONPATH=str(self.runtime),
                   LITELLM_LOCAL_MODEL_COST_MAP='true')
        self.proxy = subprocess.Popen(args, cwd=self.runtime, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding='utf-8', errors='replace', start_new_session=True)

        def capture():
            for line in self.proxy.stdout:
                for secret in self._secrets:
                    line = line.replace(secret, '<密钥已隐藏>')
                self.log.write(line)
                self.log.flush()
        self._log_thread = threading.Thread(target=capture, daemon=True)
        self._log_thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(90):
            if self.proxy.poll() is not None:
                raise RuntimeError('本讲 Chat Completions 适配器启动失败')
            try:
                with opener.open(f'http://127.0.0.1:{port}/health/liveliness', timeout=1):
                    return
            except OSError:
                time.sleep(1)
        raise TimeoutError('本讲 Chat Completions 适配器健康检查超时')

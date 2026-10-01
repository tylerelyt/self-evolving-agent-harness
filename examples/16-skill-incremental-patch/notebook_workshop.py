"""Notebook 通过管道分幕操作真实 Hermes 服务；不额外启动协议适配器。"""
import atexit
import json
import os
from pathlib import Path
import subprocess
from common import HERE


class NotebookWorkshop:
    def __init__(self, auto_approve=None):
        self.auto_approve = (os.environ.get("LAB_AUTO_APPROVE", "1") == "1"
                             if auto_approve is None else auto_approve)
        self.worker = None
        self.closed = False
        atexit.register(self.close)

    def prepare(self):
        if self.worker is not None:
            self.close()
            self.closed = False
            atexit.register(self.close)
        from credentials import load_credentials
        if not os.environ.get("LAB_CONTINUE_RUN"):
            load_credentials()
        env = os.environ.copy()
        env.pop("SKILL_LAB_RUNTIME", None)
        source = Path(env.get("HERMES_SRC", HERE.parents[1]/".deps/hermes-agent")).resolve()
        python = env.get("HERMES_PYTHON", str(source/".venv/bin/python"))
        env.update(HERMES_SRC=str(source), HERMES_PYTHON=python,
                   PYTHONDONTWRITEBYTECODE="1",
                   LAB_AUTO_APPROVE="1" if self.auto_approve else "0",
                   PYTHONPATH=str(source) + os.pathsep + env.get("PYTHONPATH", ""))
        try:
            self.worker = subprocess.Popen(
                [python, "-B", "-u", str(HERE/"notebook_worker.py")],
                cwd=HERE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            return self.step("prepare")
        except BaseException:
            self.close()
            raise

    def step(self, action, **fields):
        try:
            self.worker.stdin.write(json.dumps({"action": action, **fields}) + "\n")
            self.worker.stdin.flush()
            line = self.worker.stdout.readline()
            if not line:
                raise RuntimeError("分幕执行器退出：" + self.worker.stderr.read())
            response = json.loads(line)
            print(response["output"], end="")
            if not response["ok"]:
                raise RuntimeError(response["error"])
            if action == "finish":
                self.close()
            return response["result"]
        except BaseException:
            self.close()
            raise

    def confirm(self, digest=None):
        if digest is None:
            digest = os.environ.get("LAB_HUMAN_APPROVAL_SHA256")
        if not os.environ.get("LAB_VALIDATION_RUN_ID") and digest is None and not self.auto_approve:
            digest = input("审阅完整候选后输入 SHA-256；空输入不采用：").strip()
        return self.step("confirm", digest=digest)

    def close(self):
        if self.closed:
            return
        try:
            if self.worker:
                if self.worker.poll() is None:
                    self.worker.stdin.close()
                    try:
                        self.worker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # SIGTERM 让执行器进入 finally，先回收其服务进程组。
                        self.worker.terminate()
                        self.worker.wait(timeout=65)
                for pipe in (self.worker.stdin, self.worker.stdout, self.worker.stderr):
                    pipe.close()
        finally:
            if not self.worker or self.worker.poll() is not None:
                self.closed = True
                atexit.unregister(self.close)

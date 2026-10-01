"""Notebook 只调度独立 Hermes 进程，不向内核导入服务依赖。"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import uuid

HERE = Path(__file__).resolve().parent
PYTHON = HERE.parents[1] / '.deps/hermes-agent/.venv/bin/python'


def run_hermes(code, *, timeout=240):
    if not PYTHON.exists():
        raise RuntimeError('请先按 README 准备固定 Hermes 环境')
    if shutil.disk_usage(HERE).free < 1024 ** 3:
        raise RuntimeError('可用空间小于1GiB，停止新增运行文件')
    run = HERE / 'runtime/native/notebook' / uuid.uuid4().hex
    run.mkdir(mode=0o700, parents=True)
    env = {name: os.environ[name] for name in (
        'PATH', 'GLM_API_KEY', 'GLM_BASE_URL', 'GLM_EMBEDDING_BASE_URL',
        'OPENVIKING_ENDPOINT', 'OPENVIKING_API_KEY',
        'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
    ) if name in os.environ}
    env.update(HOME=str(run), PYTHONDONTWRITEBYTECODE='1',
               NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1',
               LITELLM_LOCAL_MODEL_COST_MAP='True', HF_HUB_OFFLINE='1')
    # 日志不进入 Notebook 输出；中断时先给 with/finally 时间清理。
    with (run / 'process.log').open('w') as log:
        process = subprocess.Popen([str(PYTHON), '-B', '-c', code], cwd=HERE,
                                   env=env, start_new_session=True, stdout=log, stderr=log)
        try:
            code = process.wait(timeout=timeout)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    if code:
        raise RuntimeError(f'Hermes 子进程退出码 {code}；日志在 {run}')
    return run

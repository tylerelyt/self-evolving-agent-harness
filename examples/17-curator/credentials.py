"""凭据只留在进程内；不读取个人配置，回放不询问。"""
import getpass
import os
from pathlib import Path
import subprocess

OFFICIAL_API = 'https://open.bigmodel.cn/api/paas/v4'
REPO = Path(__file__).resolve().parents[2]
SRC = Path(os.environ.get('HERMES_SRC', REPO / '.deps/hermes-agent')).resolve()


def verify_dependency():
    pinned = (REPO / 'scripts/hermes-revision.txt').read_text().strip()
    actual = subprocess.check_output(['git', '-C', str(SRC), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '--no-optional-locks', '-C', str(SRC), 'status', '--porcelain', '--untracked-files=normal'], text=True)
    if actual != pinned or dirty.strip():
        raise RuntimeError('Hermes 依赖不是固定且未修改的提交；停止，不自动修改依赖')
    return actual


def clean_environment():
    os.environ.pop('BIGMODEL_API_KEY', None)
    for name in list(os.environ):
        if name.lower().endswith('_proxy'):
            os.environ.pop(name, None)
    # Hermes 的显式 provider/base_url 优先；个人 Home 不进入教学进程。
    os.environ['GLM_BASE_URL'] = os.environ.get('GLM_BASE_URL') or OFFICIAL_API


def load_credentials():
    clean_environment()
    if os.environ.get('LAB_VALIDATION_RUN_ID'):
        return 0
    prompts = 0
    if not os.environ.get('GLM_API_KEY', '').strip():
        key = getpass.getpass('请输入 GLM API key（隐藏输入，只在本进程使用）：').strip()
        if not key:
            raise ValueError('未输入 GLM key，尚未请求模型')
        os.environ['GLM_API_KEY'] = key
        prompts = 1
    return prompts

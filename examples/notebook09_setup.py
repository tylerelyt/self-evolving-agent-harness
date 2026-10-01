"""09 讲的一键准备；离线不安装依赖、不启动模型连接。"""
import getpass
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import subprocess
import sys

_active_story = None


def prepare():
    global _active_story
    if _active_story is not None and not _active_story.closed:
        _active_story.close()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("请选择 Python 3.12 内核，再从头运行。")
    if not os.environ.get("LAB_VALIDATION_RUN_ID"):
        missing, restart = [], False
        for package, expected in (("openai-codex", "0.154.0"), ("litellm", "1.101.0")):
            try:
                installed = version(package)
            except PackageNotFoundError:
                installed = None
            if installed != expected:
                missing.append(("litellm[proxy]" if package == "litellm" else package) + "==" + expected)
                restart |= package.replace("-", "_") in sys.modules
        try:
            import socksio
        except ImportError:
            missing.append("httpx[socks]>=0.27,<1")
        if missing:
            subprocess.run([sys.executable, "-m", "pip", "install", *missing],
                           check=True, capture_output=True, text=True)
        if restart:
            raise RuntimeError("依赖已安装；请重启内核并重新运行，避免混用旧模块。")
        if not os.environ.get("GLM_API_KEY", "").strip():
            os.environ["GLM_API_KEY"] = getpass.getpass("GLM_API_KEY：").strip()
            if not os.environ["GLM_API_KEY"]:
                raise ValueError("GLM_API_KEY 为空，请重新运行准备格并输入密钥。")
    from notebook09_story import IsolationApprovalStory
    _active_story = IsolationApprovalStory(Path(__file__).parent / "09-isolation-approval")
    return _active_story

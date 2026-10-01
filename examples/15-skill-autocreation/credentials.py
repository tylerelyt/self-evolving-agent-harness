"""已有 GLM 环境变量优先；缺少时用 getpass 输入，不读个人配置文件。"""
import getpass
import os

def load_credentials():
    os.environ.pop('BIGMODEL_API_KEY', None)
    for name in list(os.environ):
        if name.lower().endswith('_proxy'):
            os.environ.pop(name, None)
    if os.environ.get('LAB_VALIDATION_RUN_ID'):
        os.environ.pop('GLM_API_KEY', None)
        return
    if not os.environ.get('GLM_API_KEY', '').strip():
        key = getpass.getpass('请输入 GLM_API_KEY（不回显、不保存）：').strip()
        if not key:
            raise ValueError('未输入 GLM_API_KEY')
        os.environ['GLM_API_KEY'] = key
        os.environ['LAB_API_KEY_PROMPTS'] = str(int(os.environ.get('LAB_API_KEY_PROMPTS', '0')) + 1)

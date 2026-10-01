"""只读取获准的 GLM 凭据与代理设置，不执行 dotenv、不输出内容。"""
import os
from pathlib import Path


def load_credentials():
    if os.environ.get('LAB_VALIDATION_RUN_ID'):
        return
    os.environ.pop('BIGMODEL_API_KEY', None)
    from dotenv import dotenv_values
    path = Path.home() / '.hermes/.env'
    allowed = {'GLM_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
    if path.exists():
        values = dotenv_values(path)
        for name in allowed:
            if values.get(name) and not os.environ.get(name):
                os.environ[name] = values[name]
    if not os.environ.get('GLM_API_KEY'):
        raise ValueError('请在 ~/.hermes/.env 或进程环境中提供 GLM_API_KEY')

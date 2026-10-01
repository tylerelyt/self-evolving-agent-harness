"""仅使用父进程显式提供的 GLM 配置，不读取个人密钥文件。"""
import os


def load_credentials():
    if os.environ.get('LAB_VALIDATION_RUN_ID'):
        return
    os.environ.pop('BIGMODEL_API_KEY', None)
    if not os.environ.get('GLM_API_KEY'):
        raise ValueError('请在进程环境中设置 GLM_API_KEY；本例不读取个人密钥文件')
    from openviking_config import checked_base, OFFICIAL_BASE
    checked_base(os.environ.get('GLM_BASE_URL', OFFICIAL_BASE))
    if os.environ.get('GLM_MODEL', 'glm-5.2') != 'glm-5.2':
        raise ValueError('本例使用 glm-5.2')

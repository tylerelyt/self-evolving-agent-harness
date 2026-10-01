"""生成无密钥配置；服务启动时才从进程环境展开凭据。"""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

HERE = Path(__file__).resolve().parent
OFFICIAL_BASE = 'https://open.bigmodel.cn/api/paas/v4'


def checked_base(value):
    url = urlsplit(value)
    official = (url.scheme == 'https' and url.hostname == 'open.bigmodel.cn'
                and url.path.rstrip('/') == '/api/paas/v4')
    loopback = url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')
    if not (official or loopback) or url.username or url.password or url.query or url.fragment:
        raise ValueError('仅允许智谱官方按量接口或显式 loopback 费用代理 base URL')
    if value.rstrip('/').endswith(('/embeddings', '/chat/completions')):
        raise ValueError('传入 base URL，不要附加 /embeddings 或 /chat/completions')
    return value.rstrip('/')


def build_config(env=None, run_dir=None):
    env = os.environ if env is None else env
    base = checked_base(env.get('GLM_BASE_URL', OFFICIAL_BASE))
    embedding = checked_base(env.get('GLM_EMBEDDING_BASE_URL', base))
    return {
        'server': {'host': '127.0.0.1', 'port': 1933,
                   'agent_evolution': {'enabled': True}},
        'storage': {'workspace': str((run_dir or HERE / 'runtime/native/server-preview') / 'data'),
                    'vectordb': {'backend': 'local'}, 'agfs': {'backend': 'local'}},
        'vlm': {'provider': 'openai', 'model': 'glm-5.2', 'api_base': base,
                'api_key': '${GLM_API_KEY}', 'max_tokens': 8000,
                'max_concurrent': 1, 'extra_request_body': {'thinking': {'type': 'disabled'}}},
        'embedding': {'dense': {'provider': 'openai', 'model': 'embedding-3',
                               'api_base': embedding, 'api_key': '${GLM_API_KEY}',
                               'dimension': 2048, 'encoding_format': 'float'}},
        'memory': {'extraction_enabled': True, 'session_skill_extraction_enabled': False},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    if args.write:
        import shutil
        if shutil.disk_usage(HERE).free < 1024 ** 3:
            parser.error('可用空间小于1GiB，停止生成')
        run = HERE / 'runtime/native/runs' / ('server-' + uuid.uuid4().hex)
        run.mkdir(mode=0o700, parents=True)
        path = run / 'ov.conf'
        config = build_config(run_dir=run)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
        print(path)
    else:
        print(json.dumps(build_config(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

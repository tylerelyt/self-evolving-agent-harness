"""本讲记录、版本和连接约定；不读取个人配置。"""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OFFICIAL_API = 'https://open.bigmodel.cn/api/paas/v4'


def connection_config(protocol):
    endpoint = os.environ.get('GLM_BASE_URL', protocol['base_url']).strip().rstrip('/')
    parsed = urlsplit(endpoint)
    local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in ('http', 'https') or parsed.scheme == 'http' and not local):
        raise ValueError('GLM_BASE_URL 须为无凭据、无查询参数的 HTTPS 地址；仅 loopback 可用 HTTP')
    return {'execution_endpoint': endpoint, 'protocol_endpoint': protocol['base_url'],
            'connection_mode': 'official-direct' if endpoint == OFFICIAL_API else 'loopback-proxy' if local else 'explicit-endpoint',
            'upstream_endpoint': OFFICIAL_API if endpoint == OFFICIAL_API else None,
            'upstream_verified': False,
            'notice': '这里只记录执行端的连接配置；代理的真实上游与预算须由服务端记录确认。'}


def verify_dependency(source):
    import subprocess
    pinned = (REPO / 'scripts/hermes-revision.txt').read_text().strip()
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '--no-optional-locks', '-C', str(source), 'status', '--porcelain', '--untracked-files=normal'], text=True)
    if actual != pinned or dirty.strip():
        raise RuntimeError('Hermes 依赖与固定提交不一致或工作树有改动；停止，不自动修改依赖')
    return actual


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted(Path(root).rglob('*')) if p.is_file()}


def source_hashes():
    return {str(p.relative_to(HERE)): digest(p) for p in sorted(HERE.rglob('*'))
            if p.is_file() and p.suffix in ('.py', '.sh', '.md', '.json', '.ipynb')
            and not any(x in p.relative_to(HERE).parts for x in ('runtime', 'validation', 'output', '__pycache__'))}


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        for name in ('GLM_API_KEY', 'BIGMODEL_API_KEY'):
            key = os.environ.get(name)
            if key:
                value = value.replace(key, '[凭证已隐去]')
        value = value.replace(str(REPO), '$PROJECT').replace(str(Path.home()), '$HOME')
    return value


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = scrub(value) if isinstance(value, str) else json.dumps(scrub(value), ensure_ascii=False, indent=2) + '\n'
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def clean_environment():
    os.environ.pop('BIGMODEL_API_KEY', None)
    for name in list(os.environ):
        if name.lower().endswith('_proxy'):
            os.environ.pop(name, None)


def credentials():
    clean_environment()
    if os.environ.get('LAB_VALIDATION_RUN_ID'):
        return 0
    if os.environ.get('GLM_API_KEY', '').strip():
        return 0
    import getpass
    key = getpass.getpass('请输入 GLM API key（隐藏输入，仅在本进程使用）：').strip()
    if not key:
        raise ValueError('没有输入 GLM key，尚未请求模型')
    os.environ['GLM_API_KEY'] = key
    return 1


def identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError('运行编号仅限字母、数字、下划线与连字符')
    return value

"""本地路径、HTTP 与记录格式；不包含模型或记忆操作。"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / 'runtime'
ACTIVE = BASE / 'active.json'


def now():
    return datetime.now(timezone.utc).isoformat()


def runtime():
    if os.environ.get('ONCALL_RUNTIME'):
        selected = Path(os.environ['ONCALL_RUNTIME']).resolve()
    elif ACTIVE.exists():
        selected = (BASE / json.loads(ACTIVE.read_text())['run']).resolve()
    else:
        selected = BASE / 'not-started'
    if not selected.is_relative_to(BASE.resolve()):
        raise ValueError('教学运行目录必须位于本 workshop 的 runtime 内，不接受个人或生产 Home')
    return selected


def observations():
    return BASE / 'observations' / runtime().name


def scrub(value):
    """本地观察记录也隐去凭据与宿主个人路径；业务 /tmp 路径保留。"""
    if isinstance(value, dict):
        return {k: ('[隐去]' if re.search(r'api_key|authorization|secret|password', k, re.I)
                    else scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        key = os.environ.get('GLM_API_KEY')
        if key:
            value = value.replace(key, '[隐去]')
        value = value.replace(str(HERE.parents[1]), '$PROJECT')
        value = value.replace(str(Path.home()), '$HOME')
    return value


def sanitize_logging():
    """在 Hermes 导入前保护它自己的文件日志，保留异常细节而隐去个人路径。"""
    import logging
    import traceback
    original = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        item = original(*args, **kwargs)
        item.msg, item.args = scrub(item.getMessage()), ()
        item.pathname = scrub(item.pathname)
        if item.exc_info:
            item.exc_text = scrub(''.join(traceback.format_exception(*item.exc_info)))
            item.exc_info = None
        if item.stack_info:
            item.stack_info = scrub(item.stack_info)
        return item

    logging.setLogRecordFactory(factory)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    path.with_suffix(path.suffix + '.tmp').write_text(text, encoding='utf-8')
    path.with_suffix(path.suffix + '.tmp').replace(path)


def record(path, value):
    save(path, scrub(value))


def http(url, data=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers=headers)
    # 所有业务与课程 HTTP 请求都是本机请求，明确绕过代理。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f'HTTP {e.code}: {body}') from None


def connection(kind='oncall'):
    meta = json.loads((runtime() / 'processes.json').read_text())
    return f"http://127.0.0.1:{meta[kind]['port']}"


def api(path, data=None):
    return http(connection() + path, data, (runtime() / 'http.token').read_text().strip())


def pay_admin(path, data):
    return http(connection('paylab') + path, data, (runtime() / 'http.token').read_text().strip())


def tool_calls(messages):
    """参数与同 tool_call_id 回执配对；不把通知当成成功回执。"""
    receipts = {m.get('tool_call_id'): m.get('content') for m in messages if m.get('role') == 'tool'}
    result = []
    for m in messages:
        for c in m.get('tool_calls') or []:
            fn = c.get('function', {})
            raw = fn.get('arguments', '{}')
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = {'unparsed': raw}
            receipt = receipts.get(c.get('id'))
            try:
                receipt = json.loads(receipt) if isinstance(receipt, str) else receipt
            except ValueError:
                pass
            result.append({'tool_call_id': c.get('id'), 'name': fn.get('name'),
                           'args': args, 'receipt': receipt})
    return result


def memory_operations(calls):
    """原生工具支持单项或 operations 批量；保留真实调用编号及整体回执。"""
    result = []
    for call in calls:
        if call['name'] == 'memory':
            for operation in call['args'].get('operations') or [call['args']]:
                result.append({**call, 'args': operation})
    return result


def completed(result):
    return bool(result.get('final_response') and result.get('completed')
                and not result.get('failed') and not result.get('error')
                and not result.get('interrupted') and not result.get('partial'))


def memory_diff(before, after):
    import difflib
    # 文件常无末尾换行；展示时补齐每行，避免删除行与新增行粘连。原快照不改。
    return ''.join(difflib.unified_diff([x + '\n' for x in before.splitlines()],
                                      [x + '\n' for x in after.splitlines()],
                                      fromfile='修订前', tofile='修订后'))

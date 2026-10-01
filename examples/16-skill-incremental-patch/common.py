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
    if os.environ.get('SKILL_LAB_RUNTIME'):
        selected = Path(os.environ['SKILL_LAB_RUNTIME']).resolve()
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
        return {k: ('[隐去]' if k != 'api_key_prompts' and re.search(r'api_key|authorization|secret|password', k, re.I)
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
    # 前台客户端与后台服务可能同时保存同一任务；各用专属临时文件。
    import secrets
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def record(path, value):
    save(path, scrub(value))


def http(url, data=None, token=None, timeout=30):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers=headers)
    # 所有业务与课程 HTTP 请求都是本机请求，明确绕过代理。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body).get('error', body)
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f'HTTP {e.code}: {body}') from None


def connection(kind='oncall'):
    meta = json.loads((runtime() / 'processes.json').read_text())
    return f"http://127.0.0.1:{meta[kind]['port']}"


def api(path, data=None, timeout=30):
    return http(connection() + path, data, (runtime() / 'http.token').read_text().strip(), timeout)


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


def receipt_object(call):
    """仅识别固定 Hermes 的已知循环告警尾注，原始回执不作改写。"""
    receipt = call.get("receipt")
    if isinstance(receipt, dict):
        return receipt
    if not isinstance(receipt, str):
        return {}
    try:
        value, end = json.JSONDecoder().raw_decode(receipt)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    tail = receipt[end:]
    warning = re.fullmatch(
        r"\n\n\[Tool loop warning: same_tool_failure_warning; count=([1-9][0-9]*); (.*)\]",
        tail, flags=re.DOTALL)
    if warning is None:
        return {}
    count, message = warning.groups()
    # aaf96885 的非 terminal 恢复提示；未知提示、hard stop 和其他尾文不采信。
    expected = (
        f"{call.get('name')} has failed {count} times this turn. This looks like a loop. "
        "Do not switch to text-only replies; keep using tools, but diagnose before retrying. "
        "First inspect the latest error/output and verify your assumptions. "
        "Try different arguments, a narrower query/path, an absolute path when relevant, "
        "or a different tool that can make progress. If the blocker is external, report "
        "the blocker after one diagnostic attempt instead of repeating the same failing path."
    )
    return value if message == expected else {}


def completed(result):
    return bool(result.get('final_response') and result.get('completed')
                and not result.get('failed') and not result.get('error')
                and not result.get('interrupted') and not result.get('partial'))

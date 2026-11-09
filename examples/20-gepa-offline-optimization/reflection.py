"""GEPA 的标准 LanguageModel 回调，仅替换请求传输，不改候选搜索算法。"""
import json
import os
import urllib.request
from urllib.parse import urlsplit

def connection():
    values = {name: os.environ.get(name, '').strip()
              for name in ('GLM_API_KEY', 'GLM_MODEL', 'GLM_BASE_URL')}
    if not all(values.values()):
        raise ValueError('显式设置 GLM_API_KEY、GLM_MODEL、GLM_BASE_URL；不自动直连任何默认端点')
    if any(char.isspace() for char in os.environ['GLM_API_KEY']):
        raise ValueError('GLM_API_KEY 须为无空白字符的凭证')
    url = values['GLM_BASE_URL'].rstrip('/')
    invalid_endpoint = 'GLM_BASE_URL 须为无凭据的 HTTPS 地址，或本机 HTTP 预算代理'
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError(invalid_endpoint) from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port == 0 or any(char.isspace() for char in url)
            or parsed.scheme not in ('http', 'https')
            or parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1')):
        raise ValueError(invalid_endpoint)
    return values['GLM_MODEL'], url


class ReflectionLM:
    def __init__(self, model, endpoint, root, limit, *, save_receipt=None):
        if save_receipt is None:
            from support import save as save_receipt
        self.model, self.endpoint, self.root, self.limit = model, endpoint, root, limit
        self.save_receipt = save_receipt
        self.calls = 0

    def __call__(self, prompt):
        if self.calls >= self.limit:
            raise RuntimeError('达到反思请求上限，停止搜索')
        key = os.environ.get('GLM_API_KEY', '')
        if not key or any(char.isspace() for char in key):
            raise ValueError('GLM_API_KEY 须为无空白字符的凭证')
        self.calls += 1
        messages = [{'role': 'user', 'content': prompt}] if isinstance(prompt, str) else prompt
        payload = {'model': self.model, 'messages': messages, 'temperature': 0,
                   'max_tokens': 4096, 'thinking': {'type': 'disabled'}}
        request = urllib.request.Request(self.endpoint + '/chat/completions',
                  data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json',
                  'Authorization': 'Bearer ' + key})
        # 不重试、不使用环境中的代理重写显式端点，也不跟随携带凭证的重定向。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=180) as response:
            result = json.load(response)
        path = self.root / f'{self.calls:03d}.json'
        receipt = {'messages': messages, 'model': self.model, 'endpoint': self.endpoint}
        if json.dumps(key, ensure_ascii=False)[1:-1] in json.dumps(result, ensure_ascii=False):
            self.save_receipt(path, {**receipt, 'status': 'credential_echo_rejected'})
            raise RuntimeError('反思响应包含连接凭证，已丢弃该响应')
        self.save_receipt(path, {**receipt, 'response': result})
        try:
            choice = result['choices'][0]
            text = choice['message'].get('content')
        except (KeyError, TypeError, IndexError, AttributeError) as exc:
            raise RuntimeError('反思接口未返回有效的模型回答') from exc
        if choice.get('finish_reason') != 'stop' or not isinstance(text, str) or not text.strip():
            raise RuntimeError('反思回答未正常完成，不将截断文本作为候选')
        return text

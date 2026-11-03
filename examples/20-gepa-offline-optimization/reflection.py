"""GEPA 的标准 LanguageModel 回调，仅替换请求传输，不改候选搜索算法。"""
import json
import os
import urllib.request
from urllib.parse import urlsplit

from support import save


def connection():
    values = {name: os.environ.get(name, '').strip()
              for name in ('GLM_API_KEY', 'GLM_MODEL', 'GLM_BASE_URL')}
    if not all(values.values()):
        raise ValueError('显式设置 GLM_API_KEY、GLM_MODEL、GLM_BASE_URL；不自动直连任何默认端点')
    url = values['GLM_BASE_URL'].rstrip('/')
    parsed = urlsplit(url)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in ('http', 'https')
            or parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('GLM_BASE_URL 须为无凭据的 HTTPS 地址，或本机 HTTP 预算代理')
    return values['GLM_MODEL'], url


class ReflectionLM:
    def __init__(self, model, endpoint, root, limit):
        self.model, self.endpoint, self.root, self.limit = model, endpoint, root, limit
        self.calls = 0

    def __call__(self, prompt):
        if self.calls >= self.limit:
            raise RuntimeError('达到反思请求上限，停止搜索')
        self.calls += 1
        messages = [{'role': 'user', 'content': prompt}] if isinstance(prompt, str) else prompt
        payload = {'model': self.model, 'messages': messages, 'temperature': 0,
                   'max_tokens': 4096, 'thinking': {'type': 'disabled'}}
        request = urllib.request.Request(self.endpoint + '/chat/completions',
                  data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json',
                  'Authorization': 'Bearer ' + os.environ['GLM_API_KEY']})
        # 不重试、不使用环境中的代理重写显式端点，也不跟随携带凭证的重定向。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=180) as response:
            result = json.load(response)
        choice = result['choices'][0]
        text = choice['message'].get('content')
        save(self.root / f'{self.calls:03d}.json', {'messages': messages, 'response': result,
             'model': self.model, 'endpoint': self.endpoint})
        if choice.get('finish_reason') != 'stop' or not isinstance(text, str) or not text.strip():
            raise RuntimeError('反思回答未正常完成，不将截断文本作为候选')
        return text

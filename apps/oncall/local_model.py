"""本机脚本模型。通过真实 HTTP 和 Hermes 工具循环验证固定故障场景。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
import re
import sys
from pathlib import Path
from threading import Thread
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from self_evolving_harness.http_server import LoopbackHTTPServer


EXPAND = '同一故障包含多项告警时，使用 all 查询目标集群的 alerts 与 traces，补全受影响服务。'
FILTER = '读取 all 查询结果后，按题面工单编号保留对应 incident_id，再判断根因与影响。'
BAD = '日志查询使用 all 集群，以收集所有环境的日志。'


def teaching_candidate(body, experience):
    """仅从实际查询和题面选择补丁，不读取评分答案。"""
    task = experience['task']
    incident = re.search(r'工单 (\w+)', task).group(1)
    calls = experience['calls']
    observed = [row for call in calls for row in call.get('records', [])]
    if FILTER not in body and any(row['incident_id'] != incident for row in observed):
        return body.rstrip() + '\n\n' + FILTER + '\n'
    if EXPAND not in body and '同一故障还出现' in task and observed:
        return body.rstrip() + '\n\n' + EXPAND + '\n'
    return None


def tool_response(message):
    receipt = json.loads(message['content'])
    return json.loads(receipt['output']) if receipt.get('output') else receipt


def decide(messages):
    task = next(m['content'] for m in reversed(messages) if m['role'] == 'user')
    match = re.search(r'工单 (\w+)。调查 ([\w-]+) 的 ([\w-]+)', task)
    if not match:
        raise ValueError('本机脚本模型仅处理预定义的故障快照任务')
    event, cluster, service = match.groups()
    tools = [tool_response(m) for m in messages if m['role'] == 'tool']
    if not tools:
        return 'skill_view', {'name': 'oncall-alert-triage'}
    skill = tools[0]['content']
    by_kind = {p['action']: p['records'] for p in tools[1:]}
    scope = 'all' if EXPAND in skill and '同一故障还出现' in task else service
    if 'alerts' not in by_kind:
        return 'terminal', {'command': f'./incidentctl alerts {cluster} {scope}'}
    if 'traces' not in by_kind:
        return 'terminal', {'command': f'./incidentctl traces {cluster} {scope}'}

    def relevant(rows):
        return [r for r in rows if r['incident_id'] == event] if FILTER in skill else rows

    traces = relevant(by_kind['traces'])
    root = traces[0]['upstream'] if traces else None
    if 'logs' not in by_kind:
        target = 'all' if BAD in skill else cluster
        return 'terminal', {'command': f'./incidentctl logs {target} {root or "all"}'}
    alerts, logs = relevant(by_kind['alerts']), relevant(by_kind['logs'])
    root = root if root and any(r['service'] == root for r in logs) else None
    return None, {'cluster': cluster, 'root_cause': root,
                  'affected_services': sorted({r['service'] for r in alerts}),
                  'status': 'confirmed' if root else 'insufficient',
                  'citations': [r['record_id'] for r in alerts + traces + logs],
                  'explanation': '调用链与上游日志支持连接资源耗尽。' if root else '当前缺少调用链或根因日志。'}


@contextmanager
def serve(respond=decide):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                if self.path != '/v1/chat/completions':
                    self.send_error(404)
                    return
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                name, value = respond(request['messages'])
                choice = {'role': 'assistant', 'content': None}
                if name:
                    choice['tool_calls'] = [{'id': 'cap-' + uuid4().hex,
                        'type': 'function', 'function': {'name': name, 'arguments': json.dumps(value)}}]
                else:
                    choice['content'] = json.dumps(value, ensure_ascii=False)
                finish = 'tool_calls' if name else 'stop'
                common = {'id': 'capstone-fixture', 'model': request['model'], 'created': 0}
                if request.get('stream'):
                    delta = {**choice}
                    if name:
                        delta['tool_calls'][0]['index'] = 0
                    chunks = [{**common, 'object': 'chat.completion.chunk', 'choices': [
                        {'index': 0, 'delta': delta, 'finish_reason': None}]},
                        {**common, 'object': 'chat.completion.chunk', 'choices': [
                        {'index': 0, 'delta': {}, 'finish_reason': finish}]}]
                    data = (''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks)
                            + 'data: [DONE]\n\n').encode()
                    content_type = 'text/event-stream'
                else:
                    data = json.dumps({**common, 'object': 'chat.completion', 'choices': [
                        {'index': 0, 'message': choice, 'finish_reason': finish}]}).encode()
                    content_type = 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                self.send_error(400, type(exc).__name__)

    with LoopbackHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}/v1'
        finally:
            server.shutdown()
            thread.join()

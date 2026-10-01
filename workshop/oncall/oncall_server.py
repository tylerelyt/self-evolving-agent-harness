#!/usr/bin/env python3
"""课程薄 HTTP 常驻服务：每会话真实 AIAgent，持久任务与串行队列。"""
import argparse
import json
import os
import queue
import signal
import sqlite3
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common import HERE, now, observations, record, runtime, scrub, tool_calls, completed, sanitize_logging


def prepare(credentials):
    # 在任何 Hermes 导入之前确定教学 Home；只挑取所需凭据和代理。
    from dotenv import dotenv_values
    values = dotenv_values(credentials)
    key = values.get('GLM_API_KEY')
    if not key:
        raise ValueError('凭据文件缺少 GLM_API_KEY；不回退 BIGMODEL_API_KEY')
    os.environ.pop('BIGMODEL_API_KEY', None)
    os.environ['GLM_API_KEY'] = key
    for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy']:
        if name in values and values[name] is not None:
            os.environ[name] = values[name]
    for name in ['NO_PROXY', 'no_proxy']:
        os.environ[name] = ','.join(filter(None, [os.environ.get(name), 'localhost,127.0.0.1,oncall,paylab,payment-api']))
    home = runtime() / 'hermes'
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text((HERE / 'config/hermes.yaml').read_text()
                                    + '\n  cwd: ' + json.dumps(str(HERE)) + '\n')
    os.environ.update(HERMES_HOME=str(home), TERMINAL_CWD=str(HERE), TERMINAL_ENV='local',
                      PATH=str(HERE) + os.pathsep + os.environ.get('PATH', ''))
    return key


class Service:
    def __init__(self, key, model, base_url):
        from run_agent import AIAgent
        from hermes_state import SessionDB
        from review_observer import ReviewObserver
        self.AIAgent, self.key, self.model, self.base_url = AIAgent, key, model, base_url
        self.db = sqlite3.connect(runtime() / 'oncall.sqlite3', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT)')
        self.lock = threading.RLock()
        self.agents, self.histories, self.tasks, self.sessions = {}, {}, {}, {}
        self.session_db = SessionDB(runtime() / 'hermes/state.db')
        self.observer = ReviewObserver(observations(), runtime() / 'hermes/memories/MEMORY.md')
        self.observer.install()
        self.queue = queue.Queue()
        self.accepting = True
        for row in self.db.execute('SELECT id,data FROM tasks'):
            task = json.loads(row[1])
            if task['status'] in ['queued', 'running']:
                task.update(status='interrupted', error='服务重启：正在执行的结果未知；不自动重跑')
            self.tasks[row[0]] = task
        for task in self.tasks.values():
            self.persist('tasks', task)
        for row in self.db.execute('SELECT id,data FROM sessions'):
            self.sessions[row[0]] = json.loads(row[1])
        # 原生 daemon 复盘重启不恢复；落盘标明未知，不伪造结束回执。
        for path in (observations() / 'reviews').glob('*.json'):
            event = json.loads(path.read_text())
            if event['status'] == 'running':
                event.update(status='interrupted', interruption='进程重启；复盘是否写盘未知')
                record(path, event)
            self.observer.events[event['review_id']] = event
        self.worker = threading.Thread(target=self.work, name='oncall-queue', daemon=True)
        self.worker.start()

    def persist(self, kind, item):
        with self.lock:
            self.db.execute(f'INSERT OR REPLACE INTO {kind} VALUES (?,?)', (item['id'], json.dumps(scrub(item), ensure_ascii=False)))
            self.db.commit()
            record(observations() / kind / (item['id'] + '.json'), item)

    def agent(self, sid):
        if sid not in self.agents:
            meta = self.sessions[sid]
            history = self.session_db.get_messages_as_conversation(sid, repair_alternation=True) if meta.get('used') else []
            agent = self.AIAgent(model=self.model, provider='glm', api_key=self.key, base_url=self.base_url,
                                 session_id=sid, session_db=self.session_db, enabled_toolsets=['terminal', 'memory'],
                                 max_iterations=8, quiet_mode=True, skip_context_files=True,
                                 tool_start_callback=self.tool_start, tool_complete_callback=self.tool_complete)
            agent._MEMORY_REVIEW_PROMPT = (HERE / 'config/memory-review-policy.md').read_text()
            if agent._memory_nudge_interval != 1 or agent._skill_nudge_interval != 0 or agent.compression_enabled:
                raise RuntimeError('教学复盘配置未生效')
            self.agents[sid], self.histories[sid] = agent, history
            meta.update(agent_instance=uuid.uuid4().hex, history_at_start=len(history),
                        memory_at_start=agent._memory_store.format_for_system_prompt('memory') or '',
                        agent_created_at=now(), restored=bool(meta.get('used')))
            self.persist('sessions', meta)
        return self.agents[sid]

    def new_session(self, sid):
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', sid):
            raise ValueError('Session 编号只用字母、数字、下划线、连字符，最多60字')
        with self.lock:
            if sid in self.sessions:
                raise ValueError('Session 已存在；新会话请用新编号')
            self.sessions[sid] = {'id': sid, 'created_at': now(), 'used': False}
            self.agent(sid)
            return self.inspect(sid)

    def inspect(self, sid):
        with self.lock:
            if sid not in self.sessions:
                raise ValueError('Session 不存在')
            agent = self.agent(sid)
            return {**self.sessions[sid], 'history_length': len(self.histories[sid]),
                    'memory_snapshot': agent._memory_store.format_for_system_prompt('memory') or '',
                    'cached_prompt': agent._cached_system_prompt,
                    'memory_turn_counter': agent._turns_since_memory}

    def submit(self, sid, message):
        with self.lock:
            if not self.accepting:
                raise ValueError('服务正在收尾，停止接收任务')
            if sid not in self.sessions:
                raise ValueError('请先创建 Session')
            task = {'id': 'task-' + uuid.uuid4().hex[:12], 'session': sid, 'message': message,
                    'created_at': now(), 'status': 'queued', 'tool_events': []}
            self.tasks[task['id']] = task
            self.persist('tasks', task)
            self.queue.put(task['id'])
            return task

    def tool_start(self, call_id, name, args):
        self.tool_event({'stage': 'start', 'tool_call_id': call_id, 'name': name, 'args': args})

    def tool_complete(self, call_id, name, args, receipt):
        self.tool_event({'stage': 'complete', 'tool_call_id': call_id, 'name': name, 'args': args, 'receipt': receipt})

    def tool_event(self, event):
        with self.lock:
            task = self.tasks[self.current_task]
            task['tool_events'].append({**event, 'at': now()})
            self.persist('tasks', task)

    def work(self):
        while True:
            tid = self.queue.get()
            if tid is None:
                self.queue.task_done()
                return
            task = self.tasks[tid]
            try:
                agent = self.agent(task['session'])
                self.current_task = tid
                agent._oncall_task = tid
                task.update(status='running', started_at=now(),
                            memory_before=self.observer.memory(), history_before=len(self.histories[task['session']]))
                self.persist('tasks', task)
                inherited_calls = {c['tool_call_id'] for c in tool_calls(self.histories[task['session']])}
                from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
                # 原生执行限制只放在前台队列线程；bg-review 在自己的线程设 memory 白名单。
                # 不删除 memory 能力/配置，原生计数和触发条件保持不变。
                set_thread_tool_whitelist({'terminal'}, deny_msg_fmt='前台只读，禁止 {tool_name}；记忆维护由任务结束后的独立后台复盘负责。')
                trigger_context = self.observer.task_context.set(tid)
                try:
                    result = agent.run_conversation(user_message=task['message'],
                                 system_message=(HERE / 'config/oncall-role.md').read_text(),
                                 conversation_history=self.histories[task['session']], task_id=tid)
                finally:
                    self.observer.task_context.reset(trigger_context)
                    clear_thread_tool_whitelist()
                # 返回已是完整历史，直接替换，不再拼接。
                self.histories[task['session']] = result.get('messages', [])
                meta = self.sessions[task['session']]
                meta['used'] = True
                self.persist('sessions', meta)
                task.update(result=result, calls=[c for c in tool_calls(result.get('messages', []))
                                                if c['tool_call_id'] not in inherited_calls],
                            status='completed' if completed(result) else 'failed')
            except Exception as e:
                task.update(status='failed', error=f'{type(e).__name__}: {e}')
            finally:
                task['ended_at'] = now()
                self.persist('tasks', task)
                # 前台完成状态先保存；串行队列等已有原生复盘，避免并行改同一记忆。
                if not self.observer.wait_existing(300):
                    self.accepting = False
                    task['queue_blocked'] = '后台仍在运行，暂停后续任务；等待超时不终止原生线程'
                    self.persist('tasks', task)
                    while not self.observer.wait_existing(5):
                        pass
                    self.accepting = True
                self.queue.task_done()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def dispatch(self):
        try:
            if self.headers.get('Authorization') != 'Bearer ' + self.server.token:
                self.reply(401, {'error': '需要本地课程令牌'})
                return
            parsed = urlparse(self.path)
            parts = parsed.path.strip('/').split('/')
            service = self.server.service
            if self.command == 'POST':
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or '{}')
                if parts == ['sessions']:
                    result = service.new_session(data['id'])
                elif parts == ['tasks']:
                    result = service.submit(data['session'], data['message'])
                elif parts == ['shutdown']:
                    service.accepting = False
                    if service.queue.unfinished_tasks or not service.observer.wait_existing(0):
                        raise ValueError('仍有前台或后台任务，停止请求未执行；请继续等待')
                    result = {'stopping': True}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    raise ValueError('未知写接口')
            elif parts == ['status']:
                result = {'healthy': True, 'pid': os.getpid(), 'model': service.model, 'provider': 'glm',
                          'base_url': service.base_url, 'accepting': service.accepting,
                          'queued': service.queue.qsize(), 'home': str(runtime() / 'hermes'),
                          'reviews': len(service.observer.list()), 'authentication_checked': False}
            elif parts == ['memory']:
                result = {'content': service.observer.memory()}
            elif parts == ['reviews']:
                task = parse_qs(parsed.query).get('after_task', [None])[0]
                result = service.observer.list(task)
            elif parts[0] == 'tasks' and len(parts) == 2:
                result = service.tasks[parts[1]]
            elif parts[0] == 'sessions' and len(parts) == 2:
                result = service.inspect(parts[1])
            else:
                raise ValueError('未知读取接口')
            self.reply(200, scrub(result))
        except (ValueError, KeyError) as e:
            self.reply(400, {'error': str(e)})
        except Exception as e:
            self.reply(500, {'error': scrub(f'{type(e).__name__}: {e}')})

    def reply(self, code, result):
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = dispatch


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--credentials-env', required=True)
    parser.add_argument('--model', default=os.environ.get('GLM_MODEL', 'glm-5.2'))
    parser.add_argument('--base-url', default=os.environ.get('GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4'))
    args = parser.parse_args()
    try:
        key = prepare(args.credentials_env)
        sanitize_logging()
        service = Service(key, args.model, args.base_url)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        server.service, server.token = service, (runtime() / 'http.token').read_text().strip()
        server.serve_forever()
        for agent in service.agents.values():
            agent.close()
    except Exception as e:
        print(scrub(f'服务启动/运行失败：{type(e).__name__}: {e}'), file=sys.stderr)
        sys.exit(1)

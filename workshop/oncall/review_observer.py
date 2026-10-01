"""一次性观察原生 bg-review；不触发复盘、不改计数、不补写或重试。"""
import copy
import contextvars
import logging
import threading
import uuid
from common import now, record, tool_calls, completed, memory_diff

_INSTALLED = False


class ReviewObserver:
    def __init__(self, directory, memory_file):
        self.directory = directory
        self.memory_file = memory_file
        self.events = {}
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.local = threading.local()
        # Hermes 原生线程传播 ContextVars；触发编号不依赖之后可被改动的父实例字段。
        self.task_context = contextvars.ContextVar('oncall_trigger_task', default=None)

    def memory(self):
        return self.memory_file.read_text() if self.memory_file.exists() else ''

    def persist(self, event):
        record(self.directory / 'reviews' / (event['review_id'] + '.json'), event)

    def install(self):
        global _INSTALLED
        if _INSTALLED:
            raise RuntimeError('进程级复盘观察包装只能安装一次')
        from agent import background_review
        from run_agent import AIAgent
        _INSTALLED = True
        real_worker = background_review._run_review_in_thread
        real_run = AIAgent.run_conversation
        observer = self

        def worker(agent, messages_snapshot, prompt):
            event = {'review_id': 'review-' + uuid.uuid4().hex[:12],
                     'parent_session': agent.session_id, 'task_id': observer.task_context.get() or getattr(agent, '_oncall_task', None),
                     'started_at': now(), 'thread': threading.current_thread().name,
                     'daemon': threading.current_thread().daemon, 'status': 'running',
                     'input': copy.deepcopy(messages_snapshot), 'prompt': prompt,
                     'memory_before': observer.memory(), 'warnings': []}
            with observer.changed:
                observer.events[event['review_id']] = event
                observer.persist(event)
                observer.changed.notify_all()
            observer.local.event = event
            try:
                # 执行、线程、工具白名单和计数完全由 Hermes 原 worker 负责。
                return real_worker(agent, messages_snapshot, prompt)
            except Exception as e:
                event['exception'] = f'{type(e).__name__}: {e}'
                raise
            finally:
                with observer.changed:
                    result = event.get('result', {})
                    event['status'] = 'completed' if completed(result) else 'failed'
                    event['ended_at'] = now()
                    event['memory_after'] = observer.memory()
                    event['diff'] = memory_diff(event['memory_before'], event['memory_after'])
                    observer.persist(event)
                    record(observer.directory / 'memory' / (event['task_id'] + '-before.md'), event['memory_before'])
                    record(observer.directory / 'memory' / (event['task_id'] + '-after.md'), event['memory_after'])
                    record(observer.directory / 'memory' / (event['task_id'] + '.diff'), event['diff'])
                    observer.changed.notify_all()
                del observer.local.event

        def run(agent, *args, **kwargs):
            event = getattr(observer.local, 'event', None)
            try:
                result = real_run(agent, *args, **kwargs)
            except Exception as e:
                if event is not None:
                    event['run_exception'] = f'{type(e).__name__}: {e}'
                raise
            if event is not None:
                event['result'] = copy.deepcopy(result)
                # 完整返回包含继承历史；只认新 tool_call_id，并按同 id 配回执。
                inherited = {c['tool_call_id'] for c in tool_calls(event['input'])}
                event['calls'] = [c for c in tool_calls(result.get('messages', []))
                                  if c['tool_call_id'] not in inherited]
                event['review_session'] = agent.session_id
            return result

        class Warnings(logging.Handler):
            def emit(self, entry):
                event = getattr(observer.local, 'event', None)
                if event is not None and entry.levelno >= logging.WARNING:
                    event['warnings'].append(entry.getMessage())

        background_review.logger.addHandler(Warnings())
        background_review._run_review_in_thread = worker
        AIAgent.run_conversation = run

    def list(self, task=None):
        with self.lock:
            return copy.deepcopy([e for e in self.events.values() if task is None or e['task_id'] == task])

    def wait_existing(self, timeout=300):
        import time
        deadline = time.monotonic() + timeout
        with self.changed:
            while any(e['status'] == 'running' for e in self.events.values()):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.changed.wait(min(left, 1))
        return True

"""观察原生派生请求、worker、工具回执与文件变化；不触发或补写复盘。"""
import copy
import json
import logging
import threading
import time


def tool_calls(messages, inherited=()):
    old = {c.get("id") for m in inherited for c in (m.get("tool_calls") or [])}
    receipts = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
    calls = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            if call.get("id") in old:
                continue
            fn = call.get("function", {})
            args, receipt = fn.get("arguments", {}), receipts.get(call.get("id"))
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                args = {"unparsed": args}
            try:
                receipt = json.loads(receipt) if isinstance(receipt, str) else receipt
            except ValueError:
                pass
            calls.append({"tool_call_id": call.get("id"), "name": fn.get("name"),
                          "args": args if isinstance(args, dict) else {"unparsed": args}, "receipt": receipt})
    return calls


def memory_calls(calls):
    return [c for c in calls if c["name"] == "memory"]


class Observer:
    def __init__(self, agent_class, background, home, save):
        self.agent_class, self.background, self.home, self.save = agent_class, background, home, save
        self.real_worker = background._run_review_in_thread
        self.real_run = agent_class.run_conversation
        self.local = threading.local()
        self.reviews, self.handles, self.ready = {}, {}, {}
        self.parents = []

    def memory(self):
        return {name: (self.home / "memories" / name).read_text(encoding="utf-8")
                if (self.home / "memories" / name).exists() else ""
                for name in ("MEMORY.md", "USER.md")}

    def install(self):
        observer = self

        def worker(parent, messages, prompt):
            key = parent._lesson11_turn
            event = observer.reviews[key]
            observer.handles[key] = threading.current_thread()
            event.update(started_at=time.time(), daemon=threading.current_thread().daemon,
                         input=copy.deepcopy(messages), prompt=prompt, memory_before=observer.memory())
            observer.ready[key].set()
            observer.local.event = event
            try:
                return observer.real_worker(parent, messages, prompt)
            except Exception as exc:
                event["exception"] = repr(exc)
                raise
            finally:
                event.update(ended_at=time.time(), memory_after=observer.memory())
                observer.save(f"reviews/{key}.json", event)
                del observer.local.event

        def run(agent, *args, **kwargs):
            event = getattr(observer.local, "event", None)
            try:
                result = observer.real_run(agent, *args, **kwargs)
            except Exception as exc:
                if event is not None:
                    event["run_exception"] = repr(exc)
                raise
            if event is not None:
                event["result"] = copy.deepcopy(result)
                event["calls"] = tool_calls(result.get("messages", []), event["input"])
            return result

        class Warnings(logging.Handler):
            def emit(self, record):
                event = getattr(observer.local, "event", None)
                if event is not None and record.levelno >= logging.WARNING:
                    event["warnings"].append(record.getMessage())

        self.handler = Warnings()
        self.background.logger.addHandler(self.handler)
        self.background._run_review_in_thread = worker
        self.agent_class.run_conversation = run

    def watch(self, agent):
        original = agent._spawn_background_review
        self.parents.append((agent, original))

        def spawn(**kwargs):
            key = agent._lesson11_turn
            event = {"spawned_at": time.time(), "memory_at_spawn": self.memory(), "warnings": []}
            self.reviews[key], self.ready[key] = event, threading.Event()
            self.save(f"reviews/{key}.json", event)
            try:
                return original(**kwargs)
            except Exception as exc:
                event["spawn_exception"] = repr(exc)
                self.save(f"reviews/{key}.json", event)
                raise
        agent._spawn_background_review = spawn

    def wait(self, key, timeout):
        if key not in self.reviews:
            return {"state": "not_triggered"}
        event = self.reviews[key]
        if "spawn_exception" in event:
            return {"state": "spawn_failed"}
        deadline = time.monotonic() + timeout if timeout else None
        remaining = lambda: max(0, deadline - time.monotonic()) if deadline else None
        if not self.ready[key].wait(remaining()):
            return {"state": "pending", "detail": "派生请求已记录，尚未观察到 worker 启动"}
        thread = self.handles[key]
        thread.join(remaining())
        if thread.is_alive():
            return {"state": "pending", "detail": "等待超时，原生 daemon 仍在运行；退出会中止它"}
        return {"state": "ended", "daemon": thread.daemon}

    def restore(self):
        self.background._run_review_in_thread = self.real_worker
        self.agent_class.run_conversation = self.real_run
        self.background.logger.removeHandler(self.handler)
        for agent, original in self.parents:
            agent._spawn_background_review = original

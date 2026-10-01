"""只记录真实复盘的输入和结果；不替换模型、触发条件、线程或工具行为。"""
import copy
import logging
import threading
import time
from contextlib import contextmanager


@contextmanager
def observe_reviews(parent):
    from agent import background_review
    from run_agent import AIAgent

    events = []
    local = threading.local()
    real_worker = background_review._run_review_in_thread
    real_run = AIAgent.run_conversation

    def worker(agent, messages, prompt):
        if agent is not parent:
            return real_worker(agent, messages, prompt)
        event = {"started_at": time.time(), "thread": threading.current_thread().name,
                 "daemon": threading.current_thread().daemon,
                 "input": copy.deepcopy(messages), "prompt": prompt, "warnings": []}
        events.append(event)
        local.event = event
        try:
            return real_worker(agent, messages, prompt)
        finally:
            event["ended_at"] = time.time()
            del local.event

    def run(agent, *args, **kwargs):
        result = real_run(agent, *args, **kwargs)
        event = getattr(local, "event", None)
        if event is not None:
            event["result"] = copy.deepcopy(result)
        return result

    class Warnings(logging.Handler):
        def emit(self, record):
            event = getattr(local, "event", None)
            if event is not None and record.levelno >= logging.WARNING:
                event["warnings"].append(record.getMessage())

    handler = Warnings()
    background_review.logger.addHandler(handler)
    background_review._run_review_in_thread = worker
    AIAgent.run_conversation = run
    try:
        yield events
    finally:
        AIAgent.run_conversation = real_run
        background_review._run_review_in_thread = real_worker
        background_review.logger.removeHandler(handler)

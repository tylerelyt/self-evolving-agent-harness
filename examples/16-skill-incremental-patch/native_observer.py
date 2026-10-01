"""只记录原生后台返回，不改变触发条件、工具执行器或写入操作。"""
import copy
import threading

from common import completed, now, tool_calls


class ReviewObserver:
    def __init__(self, job):
        self.job = job
        self.result = {}

    def install(self):
        from run_agent import AIAgent
        self.original = AIAgent.run_conversation
        observer = self

        def observe(agent, *args, **kwargs):
            result = observer.original(agent, *args, **kwargs)
            if threading.current_thread().name == "bg-review":
                inherited = {call["tool_call_id"] for call in tool_calls(observer.job["snapshot"])}
                observer.result = {
                    "result": copy.deepcopy(result),
                    "calls": [call for call in tool_calls(result.get("messages", []))
                              if call["tool_call_id"] not in inherited],
                    "model_requests": agent._api_call_count,
                    "thread": threading.current_thread().name,
                    "completed": completed(result),
                    "ended_at": now(),
                    "inherited_call_ids": sorted(inherited),
                }
            return result

        AIAgent.run_conversation = observe

    def restore(self):
        from run_agent import AIAgent
        AIAgent.run_conversation = self.original

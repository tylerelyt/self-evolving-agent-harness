"""学习进程内观察真实原生 bg-review 的 run_conversation；不替换模型或写入工具。"""
import copy
import threading
from common import now, tool_calls, completed

class ReviewObserver:
    def __init__(self, job):
        self.job=job; self.result={}
    def install(self):
        from run_agent import AIAgent
        self.original=AIAgent.run_conversation
        observer=self
        def observe(agent,*args,**kwargs):
            result=observer.original(agent,*args,**kwargs)
            if threading.current_thread().name=='bg-review':
                inherited={x['tool_call_id'] for x in tool_calls(observer.job['snapshot'])}
                observer.result={'result':copy.deepcopy(result),'calls':[x for x in tool_calls(result.get('messages',[])) if x['tool_call_id'] not in inherited],
                     'model_requests':agent._api_call_count,'thread':threading.current_thread().name,
                     'completed':completed(result),'ended_at':now(),
                     'inherited_call_ids':sorted(inherited)}
            return result
        AIAgent.run_conversation=observe
    def restore(self):
        from run_agent import AIAgent
        AIAgent.run_conversation=self.original

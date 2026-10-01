"""隔离学习进程内观察原生复盘，并将本实例的维护工具按原生串行路径执行。"""
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
            if threading.current_thread().name=='bg-review':
                # 原生并行读取的 ContextVar 变更留在子线程，不能成为随后写入的读取标记。
                # 本讲只对当前复盘实例选择原生 sequential 执行器；不改工具、白名单或上游文件。
                def serial(message,messages,effective_task_id,api_call_count=0):
                    agent._executing_tools=True
                    try:
                        return agent._execute_tool_calls_sequential(message,messages,effective_task_id,api_call_count)
                    finally:
                        agent._executing_tools=False
                agent._execute_tool_calls=serial
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

"""构造连接池现场：指标和日志来自实际执行，test 重放同一段处理代码。"""
import inspect
import json
import subprocess
import sys
from pathlib import Path

TITLE = "连接泄漏调查"
SEED_NAME = "database-pool-capacity-check"
SEED = """---
name: database-pool-capacity-check
description: 只读观察连接池占用和容量，不判断代码分支的泄漏原因。
---
# 连接池容量观察
查看当前占用、最大容量和请求量，区分容量不足与连接未归还。
"""
TASK = "构造服务 alpha 的请求量没有上涨，连接池占用却持续增加。请用当前工具调查，重放正常与异常路径确认原因；不得修改业务文件。"
FUTURE_TASK = "构造服务 beta 出现类似连接池耗尽。请自行查找可用方法，核对当前现场并用测试重放确认；不要沿用旧服务结论。"
ANSWER_FORMAT = "最后给JSON：branch为exception或early_return；leaked_connections与normal_leaked_connections来自异常及正常测试；note说明判断依据。"
INITIAL_CASE = "alpha"
FUTURE_CASES = [("beta", FUTURE_TASK)]
FOCUS = """本次复盘连接泄漏调查，从实际回执寻找有效技术、调试路径、工具经验与用户纠正。
先 skills_list，再 skill_view 阅读最接近的现有方法，查重后再决定创建或不创建，不预设名称。
Procedure first：主文件写通用步骤、判断条件与核验方法，教训嵌入相应步骤；工具字段与使用细节放 references/ 并在主文件引用。
不要保存单次服务名、运行编号、临时环境错误或未经测试确认的失败操作。仅保存经过本次对照重放支持的方法；没有可靠流程就不创建。
由原生 skill_manage 写主文件及引用，核对回执，并分别 skill_view 回读。不写业务文件。"""
SCHEMA = {"name": "investigate", "description": "只读构造现场：metrics/log/code 查看服务状态与代码，test 实际重放正常或异常路径，不修改业务。",
          "parameters": {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["metrics", "log", "code", "test"]},
              "case": {"type": "string", "enum": ["alpha", "beta"]},
              "scenario": {"type": "string", "enum": ["normal", "exception"]}},
              "required": ["action", "case"]}}


class Pool:
    def __init__(self):
        self.borrowed = 0
        self.acquired = 0
        self.returned = 0

    def acquire(self):
        self.borrowed += 1
        self.acquired += 1
        return object()

    def release(self, connection):
        self.borrowed -= 1
        self.returned += 1


def alpha_request(pool, abnormal):
    connection = pool.acquire()
    if abnormal:
        raise ValueError("构造解码异常")
    pool.release(connection)
    return "ok"


def beta_request(pool, abnormal):
    connection = pool.acquire()
    if abnormal:
        return "构造校验未通过，提前返回"
    pool.release(connection)
    return "ok"


def replay(case, scenario, count=4):
    if case not in ("alpha", "beta") or scenario not in ("normal", "exception"):
        raise ValueError("未知构造重放条件")
    pool = Pool()
    events = []
    handler = alpha_request if case == "alpha" else beta_request
    for i in range(count):
        try:
            outcome = handler(pool, scenario == "exception")
        except ValueError as error:
            outcome = str(error)
        events.append({"request": i+1, "outcome": outcome, "borrowed_after": pool.borrowed})
    return {"constructed": True, "case": case, "scenario": scenario, "requests": count,
            "acquired": pool.acquired, "returned": pool.returned, "borrowed": pool.borrowed,
            "events": events}


def query(action, case, scenario="exception"):
    if case not in ("alpha", "beta"):
        raise ValueError("未知构造服务")
    if action == "test":
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
             "--replay", case, scenario], capture_output=True, text=True, check=True,
            timeout=10, env={})
        return {"success": True, "executed": True, "read_only": True,
                "result": json.loads(result.stdout)}
    normal, abnormal = replay(case, "normal"), replay(case, "exception")
    if action == "metrics":
        fields = ("requests", "acquired", "returned", "borrowed")
        result = {"normal": {k: normal[k] for k in fields},
                  "incident": {k: abnormal[k] for k in fields}}
    elif action == "log":
        result = abnormal["events"]
    elif action == "code":
        result = inspect.getsource(Pool) + "\n" + inspect.getsource(alpha_request if case == "alpha" else beta_request)
    else:
        raise ValueError("只支持 metrics/log/code/test")
    return {"success": True, "constructed": True, "read_only": True, "case": case, "result": result}


def verify(calls, case):
    from common import receipt_object
    calls = [c for c in calls if c["name"] == "investigate" and c["args"].get("case") == case]
    actions = {c["args"].get("action") for c in calls if receipt_object(c).get("success")}
    tests = {c["args"].get("scenario"): receipt_object(c).get("result", {})
             for c in calls if c["args"].get("action") == "test"
             and receipt_object(c).get("executed")
             and isinstance(receipt_object(c).get("result"), dict)}
    return ({"metrics", "log", "code", "test"} <= actions
            and tests.get("normal", {}).get("borrowed") == 0
            and tests.get("exception", {}).get("borrowed") == 4
            and tests.get("exception", {}).get("returned") == 0)


def verify_answer(answer, case):
    return (answer.get("branch") == ("exception" if case == "alpha" else "early_return")
            and answer.get("leaked_connections") == 4
            and answer.get("normal_leaked_connections") == 0)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--replay":
        print(json.dumps(replay(sys.argv[2], sys.argv[3]), ensure_ascii=False))
    else:
        raise SystemExit("仅供 test 工具进行隔离重放")

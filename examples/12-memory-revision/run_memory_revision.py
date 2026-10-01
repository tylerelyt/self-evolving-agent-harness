#!/usr/bin/env python3
"""第 12 讲：真实 Hermes 在线材料 → 自动后台修订 → 新会话使用规则。

这几行是核心：查找下文相同标记。业务服务是本地样例；复盘不是自建循环。
每次使用全新教学 HERMES_HOME，原有运行目录和个人记忆不动。
"""
import difflib
import json
import os
import shlex
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES_SRC = Path(os.environ.get("HERMES_SRC", HERE.parents[1] / ".deps/hermes-agent")).resolve()
OUT = Path(os.environ.get("LECTURE12_OUTPUT", HERE / "output" / time.strftime("%Y%m%d-%H%M%S"))).resolve()
OUT.mkdir(parents=True, exist_ok=True)
# 不接受生产 HERMES_HOME：教学数据始终放在本练习内的全新子目录。
HOME = HERE / ".hermes-home" / OUT.name
HOME.mkdir(parents=True, exist_ok=False)
os.environ["HERMES_HOME"] = str(HOME)
os.environ["TERMINAL_CWD"] = str(OUT)
os.environ["TERMINAL_ENV"] = "local"
sys.path.insert(0, str(HERMES_SRC))

# 这几行是核心（1）：真实配置启用文件记忆，每 1 个用户 turn 检查；禁用 Skill 复盘。
(HOME / "config.yaml").write_text("""memory:
  memory_enabled: true
  user_profile_enabled: false
  nudge_interval: 1
  memory_char_limit: 2200
  write_approval: false
skills:
  creation_nudge_interval: 0
terminal:
  backend: local
compression:
  enabled: false
""", encoding="utf-8")

from run_agent import AIAgent  # noqa: E402
from tools.memory_tool import ENTRY_DELIMITER, MemoryStore, memory_tool  # noqa: E402
from observe_review import observe_reviews  # noqa: E402

MEM = HOME / "memories/MEMORY.md"
OLD = "支付服务的生产日志统一写入 payment-api-prod。日志时间戳采用 UTC。"
KEEP = "支付告警达到 P2 时应通知当班值守人员。"
# 这几行是核心（2）：业务规则由接入方补充。真实默认提示主要关心用户偏好。
REVIEW_POLICY = """复盘上述巡检会话，检查已有长期记忆是否仍正确。
只有正式、已确认的迁移说明才能推翻旧规则；一次临时地址查成功不能成为长期规则。
把正式说明与启动时的记忆逐条对照，自行判断有无需要修订的条目，选择合适的 memory 操作。
修订时存按服务名、区域、环境查询 Service Catalog（服务目录）的规则，
保留查询当前入口及历史归档的方法，不存本次返回的地址、请求编号或任务过程。
替换会改整条记录，旧条目中没有失效的字段也要一并保留；无关条目不动。
最后简短说明修改依据；没有确凿依据就不写入。
"""


def save(name, value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    (OUT / name).write_text(text + "\n", encoding="utf-8")


def memory_calls(result):
    calls = []
    receipts = {m.get("tool_call_id"): m.get("content") for m in result.get("messages", [])
                if m.get("role") == "tool"}
    for message in result.get("messages", []):
        for call in message.get("tool_calls") or []:
            if call.get("function", {}).get("name") == "memory":
                args = json.loads(call["function"]["arguments"])
                calls.append({"args": args, "receipt": json.loads(receipts[call["id"]])})
    return calls


def completed(result):
    assert not result.get("error") and not result.get("interrupted"), result.get("error")
    assert result.get("final_response") and result.get("turn_exit_reason"), "未正常完成一轮"


def tool_checks():
    """手动给真工具传参，只验证工具约束；不把这些调用当作模型自主决策。"""
    store = MemoryStore()
    store.load_from_disk()
    def call(**args):
        result = json.loads(memory_tool(store=store, target="memory", **args))
        assert result.get("success"), result
        return result
    call(action="add", content=OLD)
    call(action="add", content=KEEP)
    call(action="add", content="payment-api-prod 另有测试用说明，勿用于生产。")
    before = MEM.read_text()
    tests = [("多条命中", "payment-api-prod", "不应写入", "Multiple entries matched"),
             ("零命中", "不存在的旧条目", "不应写入", "No entry matched"),
             ("容量超限", OLD, "长" * (store.memory_char_limit + 1), "Replacement would put")]
    results = []
    for label, old, content, error in tests:
        result = json.loads(memory_tool(action="replace", target="memory", old_text=old,
                                        content=content, store=store))
        assert result.get("success") is False and error in result.get("error", ""), result
        assert MEM.read_text() == before, "失败调用意外改了磁盘"
        results.append({"case": label, "result": result, "disk_unchanged": True})
        print(f"[真工具约束] {label}: {result['error']}", flush=True)
    # 同一工具 remove 清理冲突样例，剩下恰好两条：旧规则和无关条目。
    call(action="remove", old_text="另有测试用说明")
    assert MEM.read_text().split(ENTRY_DELIMITER) == [OLD, KEEP]
    save("tool-checks.json", results)


def make_agent(key):
    agent = AIAgent(model=os.environ.get("GLM_MODEL", "glm-5.2"), provider="glm",
                   api_key=key, base_url=os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
                   max_iterations=8, quiet_mode=True, enabled_toolsets=["memory", "terminal"],
                   skip_context_files=True)
    assert agent._memory_nudge_interval == 1 and agent._skill_nudge_interval == 0
    assert "memory" in agent.valid_tool_names and "terminal" in agent.valid_tool_names
    return agent


def run_with_review(agent, events, label, prompt, history, system):
    count = len(events)
    result = agent.run_conversation(user_message=prompt, conversation_history=history,
                                    system_message=system)
    save(f"{label}-foreground.json", result)
    completed(result)
    assert not memory_calls(result), "前台写了记忆，不能用来证明后台修订"
    deadline = time.monotonic() + int(os.environ.get("REVIEW_TIMEOUT", "300"))
    # 这里只等 Hermes 的真实线程结束，没有自行执行复盘或补写记忆。
    while len(events) <= count or "ended_at" not in events[count]:
        if time.monotonic() >= deadline:
            save(f"{label}-review.json", events[count:] or {"error": "未观察到真实复盘启动"})
            raise TimeoutError("后台未在等待期限内完成，不能宣布成功")
        time.sleep(0.2)
    review = events[count]
    save(f"{label}-review.json", review)
    completed(review.get("result", {}))
    assert review["thread"] == "bg-review" and review["daemon"], review
    print(f"[{label}] 前台回答：{result['final_response']}", flush=True)
    print(f"[{label}] 真实后台回执：{json.dumps(memory_calls(review['result']), ensure_ascii=False)}", flush=True)
    return result, review


def catalog(version):
    return {f"payment-api/{region}/{env}": {
                "current": f"logs://payment-api/{region}/{env}/v{version}",
                "archive": f"archive://payment-api/{region}/{env}"}
            for region in ("ap-south", "eu-west") for env in ("prod", "staging")}


def check_local():
    """不请求模型；只验证真实 memory 工具与 AIAgent 启动/重建的实现。"""
    tool_checks()
    # 仅供初始化；这个分支绝不调用 run_conversation，不发送 API 请求。
    key = os.environ.get("GLM_API_KEY") or os.environ.get("BIGMODEL_API_KEY") or "unused-no-api-requests"
    old_agent = make_agent(key)
    new_agent = None
    try:
        before = MEM.read_text()
        old_snapshot = old_agent._memory_store.format_for_system_prompt("memory")
        old_agent._cached_system_prompt = old_agent._build_system_prompt()
        replacement = ("支付日志按区域和环境分别存储。通过 Service Catalog 服务目录，"
                       "按服务名、区域和环境查当前入口及历史归档。日志时间戳采用 UTC。")
        result = json.loads(memory_tool(action="replace", target="memory", old_text="payment-api-prod",
                                        content=replacement, store=old_agent._memory_store))
        assert result.get("success"), result
        after = MEM.read_text()
        assert after.split(ENTRY_DELIMITER) == [replacement, KEEP]
        assert old_agent._memory_store.format_for_system_prompt("memory") == old_snapshot
        assert OLD in old_agent._cached_system_prompt
        new_agent = make_agent(key)
        new_snapshot = new_agent._memory_store.format_for_system_prompt("memory")
        assert replacement in new_snapshot and OLD not in new_snapshot
        assert replacement in new_agent._build_system_prompt()
        # 当前源码的例外：显式失效/重建系统提示时会重新加载，压缩也走此路径。
        old_agent._invalidate_system_prompt()
        assert replacement in old_agent._memory_store.format_for_system_prompt("memory")
        assert replacement in old_agent._build_system_prompt()
        save("memory-before.md", before)
        save("memory-after.md", after)
        save("memory.diff", "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                  fromfile="MEMORY-before", tofile="MEMORY-after")))
        save("summary.json", {"status": "local_checks_passed", "model_requests": 0,
             "replace_receipt": result, "old_snapshot_unchanged": True,
             "new_snapshot_loaded": True, "explicit_invalidation_reloads": True,
             "autonomous_review": "未实跑；本分支手动调用真工具，不是模型自主修订"})
        print("[本地核验通过] 真工具唯一匹配替换；无关条目保留；旧快照不变；新 AIAgent 重读。")
        print("[源码例外已核验] 显式使系统提示失效会重新加载；本次没有实际触发上下文压缩。")
        print("[模型链路未实跑] 本模式没有模型请求，不证明后台自主修订或新会话业务行为。")
    finally:
        old_agent.close()
        if new_agent is not None:
            new_agent.close()


def main():
    key = os.environ.get("GLM_API_KEY") or os.environ.get("BIGMODEL_API_KEY")
    if not key:
        raise RuntimeError("未实跑：需要 GLM_API_KEY 或 BIGMODEL_API_KEY")
    print("输出目录：", OUT, flush=True)
    tool_checks()
    before = MEM.read_text()
    save("memory-before.md", before)
    save("catalog.json", catalog(1))
    cli = f"python3 {shlex.quote(str(HERE / 'log_service.py'))}"
    system = ("你是支付巡检 Agent。用中文回答。在线任务只处理告警，不调用 memory 写入；"
              "事后记忆维护交给后台复盘。本教学环境的业务 CLI 是："
              f"{cli} catalog --data {shlex.quote(str(OUT))} --service 服务名 --region 区域 --env 环境；"
              f"{cli} logs --data {shlex.quote(str(OUT))} --service 服务名 --region 区域 --env 环境 --entry 入口。"
              "catalog 返回 current 当前入口和 archive 历史归档；logs 返回指定告警时段日志。"
              "在线阶段只用 terminal 执行业务查询，不用它修改文件或执行其他命令；"
              "后台复盘可以按独立复盘规则使用 memory 工具。")
    # 这几行是核心（3）：旧条目先入盘，再构造 AIAgent，启动快照才真正含有旧记忆。
    agent = make_agent(key)
    agent._MEMORY_REVIEW_PROMPT = REVIEW_POLICY
    snapshot = agent._memory_store.format_for_system_prompt("memory")
    assert OLD in snapshot
    try:
        with observe_reviews(agent) as events:
            temp, temp_review = run_with_review(agent, events, "01-temporary",
                "值班记录：同事给了 temporary://incident-812，本次临时读到了支付日志。"
                "没有正式变更通知，不清楚以后能否复用。请判断这一次成功能说明什么，"
                "仅回答，不再查询。", None, system)
            assert MEM.read_text() == before, "临时成功被错误沉淀为长期记忆"
            assert not memory_calls(temp_review["result"]), "负对照应不写入记忆"
            # 这几行是核心（4）：在线留正式材料，不给模型指定 old_text 或 replace。
            migration = ("【正式迁移说明 MIG-20261010，由平台团队确认生效】支付服务日志已按区域、"
                         "环境分开存储，原统一入口停止作为查询依据。以后以 service、region、env "
                         "查询 Service Catalog（服务目录），取得当前入口；历史日志也从目录找归档。"
                         "地址可能继续变化，不应把本次返回的具体地址视为长期规则。"
                         "【告警 A-812】service=payment-api，region=ap-south，env=prod，时段02:10-02:15。"
                         "请读取告警窗口日志，说明排查结果和采用的依据。")
            current, revision = run_with_review(agent, events, "02-migration", migration,
                                                temp["messages"], system)
            after = MEM.read_text()
            save("memory-after.md", after)
            diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile="MEMORY-before", tofile="MEMORY-after"))
            save("memory.diff", diff)
            print("[磁盘 diff]\n" + diff, flush=True)
            calls = memory_calls(revision["result"])
            # 这几行是核心（5）：依靠真实工具回执，而非“记忆已更新”通知判断成功。
            replacements = [(op, c["receipt"]) for c in calls
                            for op in (c["args"].get("operations") or [c["args"]])
                            if op.get("action") == "replace"]
            assert len(replacements) == 1, "应替换一条旧规则；不能新旧并存或悄悄由脚本补写"
            op, receipt = replacements[0]
            assert receipt.get("success") and op.get("old_text"), receipt
            assert sum(op["old_text"] in e for e in before.split(ENTRY_DELIMITER)) == 1
            entries = after.split(ENTRY_DELIMITER)
            assert len(entries) == 2 and KEEP in entries and OLD not in entries
            rule = next(e for e in entries if e != KEEP)
            assert all(word in rule for word in ("区域", "环境", "归档", "UTC")) and (
                "目录" in rule or "Catalog" in rule)
            assert all(value not in after for value in ("payment-api-prod", "logs://", "temporary://", "A-812"))
            # 这几行是核心（6）：磁盘已改，现有 Memory 快照和系统提示仍保持旧内容。
            assert agent._memory_store.format_for_system_prompt("memory") == snapshot
            assert OLD in agent._cached_system_prompt and rule not in agent._cached_system_prompt
            save("snapshot-old.txt", snapshot)
            print("[旧会话] 磁盘已更新；Memory 快照和缓存系统提示仍含旧条目。", flush=True)
    finally:
        agent.close()

    # 业务目录再次换地址；新告警换区域，输入不再含迁移说明或查询步骤。
    prior_events = [json.loads(line) for line in (OUT / "service-events.jsonl").read_text().splitlines()]
    assert [e["action"] for e in prior_events] == ["catalog", "logs"], prior_events
    save("catalog.json", catalog(2))
    # 这几行是核心（7）：同一配置、新 AIAgent 自动重读磁盘；不手动刷新旧会话。
    fresh = make_agent(key)
    fresh._memory_nudge_interval = 0  # 新会话只核验前台使用，避免第二次复盘干扰文件。
    try:
        new_snapshot = fresh._memory_store.format_for_system_prompt("memory")
        assert rule in new_snapshot and OLD not in new_snapshot
        save("snapshot-new.txt", new_snapshot)
        result = fresh.run_conversation(
            user_message="【告警 A-913】service=payment-api，region=eu-west，env=staging，时段02:10-02:15。请排查。",
            system_message=system)
        save("03-new-session.json", result)
        completed(result)
        assert not memory_calls(result)
        assert rule in fresh._cached_system_prompt and OLD not in fresh._cached_system_prompt
        service_events = [json.loads(line) for line in (OUT / "service-events.jsonl").read_text().splitlines()]
        new_events = service_events[len(prior_events):]
        expected = catalog(2)["payment-api/eu-west/staging"]["current"]
        assert [e["action"] for e in new_events] == ["catalog", "logs"], new_events
        assert new_events[1]["entry"] == expected and all(e["key"] == "payment-api/eu-west/staging" for e in new_events)
        assert MEM.read_text() == after
        print("[新会话回答]", result["final_response"], flush=True)
        print("[新会话真实业务调用] 先查目录，再读", expected, flush=True)
        save("summary.json", {"status": "passed", "model": fresh.model,
             "temporary_evidence_unchanged": True, "native_background_replace": True,
             "old_snapshot_unchanged": True, "new_snapshot_loaded": True,
             "new_session_catalog_then_logs": True, "business_service": "本地教学样例",
             "home": str(HOME), "output": str(OUT)})
    finally:
        fresh.close()
    print("全部断言通过；summary.json、后台输入与回执、diff 和业务查询记录已保存。", flush=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check-local"]:
            check_local()
        elif sys.argv[1:]:
            raise ValueError("只支持无参数完整运行或 --check-local 本地核验")
        else:
            main()
    except Exception as exc:
        save("summary.json", {"status": "failed", "error": str(exc), "type": type(exc).__name__})
        raise

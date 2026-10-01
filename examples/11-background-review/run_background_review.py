#!/usr/bin/env python3
"""第 11 讲练习：后台复盘是异步 best-effort，不阻塞前台。

用真实的 Hermes AIAgent + glm-5.2 跑一个对话，分别观察：
  1. 前台返回时后台是否仍在运行；前台无需等待复盘。
  2. 实际启动的线程是否为 daemon；线程结束、工具结果和文件变化分别记录。
  3. 本练习观察两个阻止触发的条件：同时关闭 Memory/Skill 自动检查；turn 被中断。

触发条件源码在 agent/turn_finalizer.py：
    if final_response and not interrupted and (_should_review_memory or _should_review_skills):
        try:
            agent._spawn_background_review(...)
        except Exception:
            pass
其中 _should_review_memory 由 agent._memory_nudge_interval 控制，
_should_review_skills 由 agent._skill_nudge_interval 控制。

运行环境（由 run.sh 负责）：
  - 沿用进程代理设置
  - export GLM_API_KEY / GLM_BASE_URL（脚本从 env 读，不硬编码 key）
  - HERMES_HOME 指向本目录下的 .hermes-home，避免污染真实配置
"""
import os
import sys
import time
import threading
import json
import copy
import logging
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HERMES_SRC = Path(
    os.environ.get("HERMES_SRC") or REPO_ROOT / ".deps" / "hermes-agent"
).expanduser().resolve()
sys.path.insert(0, str(HERMES_SRC))
os.chdir(HERMES_SRC)

# 把 HERMES_HOME 钉在本练习自带目录，MEMORY.md / state.db 都落在这里。
os.environ["HERMES_HOME"] = str(HERE / ".hermes-home")
(HERE / ".hermes-home").mkdir(parents=True, exist_ok=True)

from run_agent import AIAgent  # noqa: E402


def banner(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def list_bg_review_threads():
    return [t for t in threading.enumerate() if t.name == "bg-review"]


def snapshot_memory():
    """读当前 MEMORY.md / USER.md 全文，用于对比前台与复盘分别写了什么。"""
    out = {}
    for label in ("MEMORY.md", "USER.md"):
        p = Path(os.environ["HERMES_HOME"]) / "memories" / label
        out[label] = p.read_text(encoding="utf-8") if p.exists() else ""
    return out


def count_foreground_memory_calls(messages):
    """统计前台这一轮里模型自己发起的 memory 工具调用次数。"""
    n = 0
    for m in messages or []:
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {}).get("name", "")
            if fn == "memory":
                n += 1
    return n


def make_recorder(agent):
    """包一层 _spawn_background_review：记录触发事件，并抓住真正的复盘线程句柄。

    注意：把普通函数赋给实例属性后，agent._spawn_background_review(...) 调用时
    不会自动绑定 self，所以 wrap 只收 **kw，并用闭包持有真实 bound 方法。
    """
    real_spawn = agent._spawn_background_review  # 此刻拿到 bound 方法
    events = {"spawned_at": None, "review_thread": None, "called_by": None,
              "started_at": None, "ended_at": None, "input": None,
              "result": None, "failures": []}
    from agent import background_review
    real_worker = background_review._run_review_in_thread
    real_run = AIAgent.run_conversation

    def observed_run(review_agent, *args, **kw):
        if threading.current_thread() is not events["review_thread"]:
            return real_run(review_agent, *args, **kw)
        try:
            result = real_run(review_agent, *args, **kw)
            events["result"] = copy.deepcopy(result)
            return result
        except Exception as exc:
            events["failures"].append(repr(exc))
            raise

    class FailureRecorder(logging.Handler):
        def emit(self, record):
            if (threading.current_thread() is events["review_thread"]
                    and record.levelno >= logging.WARNING):
                events["failures"].append(record.getMessage())

    handler = FailureRecorder()
    background_review.logger.addHandler(handler)

    def observed_worker(parent, messages, prompt):
        events["review_thread"] = threading.current_thread()
        events["started_at"] = time.time()
        events["input"] = {"messages_snapshot": copy.deepcopy(messages), "prompt": prompt}
        try:
            return real_worker(parent, messages, prompt)
        finally:
            events["ended_at"] = time.time()

    # 仅在本练习进程内埋点，不改 Hermes 源码。
    background_review._run_review_in_thread = observed_worker
    AIAgent.run_conversation = observed_run

    def wrap(**kw):
        events["spawned_at"] = time.time()
        events["called_by"] = threading.current_thread().name
        try:
            real_spawn(**kw)
        except Exception as exc:
            events["failures"].append(repr(exc))
            raise
        print(f"  [复盘触发] 主循环线程={events['called_by']}；实际启动另行核对")

    return events, wrap


def review_tool_results(events):
    """按调用编号排除前台旧回执，成功与否只依据工具返回。"""
    old_ids = {m.get("tool_call_id") for m in (events.get("input") or {}).get("messages_snapshot", [])
               if m.get("role") == "tool"}
    results = []
    for m in (events.get("result") or {}).get("messages", []):
        if m.get("role") != "tool" or m.get("tool_call_id") in old_ids:
            continue
        try:
            data = json.loads(m.get("content", ""))
        except (ValueError, TypeError):
            data = None
        results.append({"tool_call_id": m.get("tool_call_id"), "receipt": m.get("content"),
                        "success": data.get("success") if isinstance(data, dict) else None})
    return results


def main():
    api_key = os.environ.get("GLM_API_KEY") or os.environ.get("BIGMODEL_API_KEY")
    base_url = os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    if not api_key:
        print("ERROR: 需要先 export GLM_API_KEY（或 BIGMODEL_API_KEY，见 run.sh）", file=sys.stderr)
        sys.exit(1)

    trace_path = HERE / "output" / "review_trace.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    # 每次先撤下旧结果，避免初始化失败时误读上一次的成功记录。
    trace_path.write_text(json.dumps({"status": "running", "started_at": time.time()},
                                    ensure_ascii=False, indent=2), encoding="utf-8")

    print("环境 HERMES_HOME =", os.environ["HERMES_HOME"])
    print("模型 glm-5.2  base_url =", base_url)

    # ------------------------------------------------------------------
    # 构造前台 agent。nudge_interval 设为 1：每一轮都满足 should_review_memory。
    # ------------------------------------------------------------------
    agent = AIAgent(
        model="glm-5.2",
        provider="glm",
        api_key=api_key,
        base_url=base_url,
        quiet_mode=True,
        max_iterations=6,
        enabled_toolsets=["hermes-cli"],
    )
    agent._memory_nudge_interval = 1
    agent._skill_nudge_interval = 0  # 只让 memory 复盘触发，排除 skill 干扰
    # 预置计数器：turn_context 里 `turns_since_memory >= nudge_interval` 才触发。
    # 首轮 turns_since_memory=0 不满足，所以手动拨到等于 interval。
    agent._turns_since_memory = agent._memory_nudge_interval

    # 包一层 recorder（闭包持有真实 bound 方法）。
    events, wrap = make_recorder(agent)
    agent._spawn_background_review = wrap

    mem_file = Path(os.environ["HERMES_HOME"]) / "memories" / "MEMORY.md"
    user_file = Path(os.environ["HERMES_HOME"]) / "memories" / "USER.md"

    # ==================================================================
    # 场景一：正常完成一轮，前台先返回，复盘在后台线程继续跑。
    # ==================================================================
    banner("场景一：正常对话，前台先返回，复盘在后台线程执行")
    print("用户：我叫 Tyler，是一名后端工程师，日常用 Python。请记住我讨厌冗长的解释，"
          "以后回答尽量直接给结论。")
    t_start = time.time()
    res = agent.run_conversation(
        user_message="我叫 Tyler，是一名后端工程师，日常用 Python。"
                     "请记住我讨厌冗长的解释，以后回答尽量直接给结论。"
    )
    t_return = time.time()
    live_at_return = list_bg_review_threads()
    print(f"[前台返回] {t_return - t_start:.2f}s  exit={res.get('turn_exit_reason')} "
          f"interrupted={res.get('interrupted')}")
    print("[前台回答]", repr((res.get("final_response") or "")[:160]))
    # 鉴权等错误可能作为 final_response 返回，非空文本不能证明前台正常完成。
    if res.get("error") or not res.get("turn_exit_reason") or res.get("interrupted"):
        failure = {
            "status": "failed",
            "case": "正常完成一轮",
            "foreground_started_at": t_start,
            "foreground_returned_at": t_return,
            "turn_exit_reason": res.get("turn_exit_reason"),
            "interrupted": res.get("interrupted"),
            "failure": res.get("error") or res.get("final_response") or "前台没有正常结束状态",
            "review_spawned": events["spawned_at"] is not None,
        }
        trace_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        agent.close()
        raise RuntimeError("前台未正常完成，停止后台复盘对照；详见本次 review_trace.json。")
    fg_mem_calls = count_foreground_memory_calls(res.get("messages"))
    print(f"前台模型本轮自己调用 memory 工具的次数 = {fg_mem_calls}")
    snap_at_return = snapshot_memory()

    print("\n-- 分别记录前台返回时与随后 2 秒内的后台线程 --")
    live = live_at_return
    # 前台刚返回时复盘线程应刚启动；轮询一小段时间确认它确实存在过。
    poll_deadline = time.time() + 2.0
    while not live and time.time() < poll_deadline:
        live = list_bg_review_threads()
        time.sleep(0.05)
    print(f"前台返回时 bg-review 线程仍存活？ {bool(live_at_return)}")
    print(f"随后 2 秒内观察到 bg-review 存活？ {bool(live)} "
          f"(存活 {len(live)} 个)")
    if events["spawned_at"] is not None:
        print(f"复盘派生时间戳 = {events['spawned_at'] - t_start:.2f}s "
              f"(晚于前台开始，紧跟在前台返回之前)")
    else:
        print("复盘派生时间戳 = None（recorder 未被调用）")

    print("\n-- 最多等待后台复盘线程 60 秒，再检查状态 --")
    rt = events["review_thread"]
    if rt is None:
        # 兜底：从 enumerate 里抓。
        ts = list_bg_review_threads()
        rt = ts[-1] if ts else None
    review_dur = 0.0
    if rt is not None:
        rt.join(timeout=60)
        t_done = time.time()
        print(f"[线程状态] {'仍在运行' if rt.is_alive() else '线程结束'}；"
              f"daemon={rt.daemon}；观察时点={t_done - t_start:.2f}s")
    else:
        t_done = time.time()
        print("(未抓到复盘线程句柄)")

    receipts = review_tool_results(events)
    print("[工具结果]", json.dumps(receipts, ensure_ascii=False))
    print("[失败原因]", events["failures"] or "未记录到失败；不能由此推断写入成功")
    print("\n-- 对比：前台刚返回时 vs 当前观察时点，记忆文件的变化 --")
    snap_after = snapshot_memory()
    for label in ("MEMORY.md", "USER.md"):
        before = snap_at_return[label]
        after = snap_after[label]
        print(f"[文件变化] {label}: {before != after}；"
              f"{len(before.encode('utf-8'))} -> {len(after.encode('utf-8'))} 字节")
        if before != after:
            print("  复盘新增/改动内容：")
            print("  " + after.strip()[:500].replace("\n", "\n  "))
        else:
            print("  当前未观察到变化；结合后台输入、线程状态和工具回执判断原因")

    trace = {k: v for k, v in events.items() if k != "review_thread"}
    trace.update(foreground_started_at=t_start, foreground_returned_at=t_return,
                 thread_alive_at_return=bool(live_at_return),
                 thread_seen_within_two_seconds=bool(live),
                 thread_daemon=rt.daemon if rt else None,
                 thread_alive=rt.is_alive() if rt else None, tool_results=receipts,
                 memory_at_return=snap_at_return, memory_at_observation=snap_after)
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    if rt is not None and rt.is_alive():
        print("线程仍在运行，停止后续对照，避免与本次复盘混在一起。进程退出可能中止复盘。")
        return

    # ==================================================================
    # 场景二：同时关闭 Memory 与 Skill 自动检查。
    # ==================================================================
    banner("场景二：同时关闭 Memory 和 Skill 自动检查，不触发复盘")
    agent._memory_nudge_interval = 0
    agent._skill_nudge_interval = 0
    events["spawned_at"] = None
    t_start2 = time.time()
    res2 = agent.run_conversation(
        user_message="今天天气怎么样？简单回我一句。"
    )
    t_return2 = time.time()
    print(f"[前台返回] {t_return2 - t_start2:.2f}s  "
          f"exit={res2.get('turn_exit_reason')} interrupted={res2.get('interrupted')}")
    print("[前台回答]", repr((res2.get("final_response") or "")[:120]))
    print(f"复盘是否被派生？ {events['spawned_at'] is not None}  "
          f"(预期 False：Memory 和 Skill 两项检查均关闭)")
    # 这几行是核心：记录实际结果，不能把配置意图当成已经验证的行为。
    disabled_check = {
        "case": "两项自动检查均关闭",
        "turn_exit_reason": res2.get("turn_exit_reason"),
        "interrupted": res2.get("interrupted"),
        "foreground_response_present": bool(res2.get("final_response")),
        "foreground_completed": bool(res2.get("turn_exit_reason")) and not res2.get("error")
                                and res2.get("interrupted") is False,
        "review_spawned": events["spawned_at"] is not None,
    }
    trace["controls"] = [disabled_check]
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    if disabled_check["review_spawned"] or not disabled_check["foreground_completed"]:
        agent.close()
        raise AssertionError("关闭检查对照未成立，请查看本次 review_trace.json。")

    # ==================================================================
    # 场景三：turn 被中断（interrupted=True），即使满足触发条件也不派生复盘。
    # ==================================================================
    banner("场景三：turn 被中断（interrupted=True），不派生复盘")
    agent._memory_nudge_interval = 1  # 重新打开，制造"本来该触发"的条件
    agent._turns_since_memory = 1
    events["spawned_at"] = None

    def fire_interrupt():
        time.sleep(1.6)
        print("  [定时器] 从另一线程调用 agent.interrupt(hard_cancel=True)")
        agent.interrupt("用户停止了", hard_cancel=True)

    threading.Thread(target=fire_interrupt, daemon=True).start()
    t_start3 = time.time()
    res3 = agent.run_conversation(
        user_message="请用 terminal 工具执行 `sleep 25`，执行完告诉我退出码。"
    )
    t_return3 = time.time()
    print(f"[前台返回] {t_return3 - t_start3:.2f}s  "
          f"exit={res3.get('turn_exit_reason')} interrupted={res3.get('interrupted')}")
    print("[前台回答]", repr((res3.get("final_response") or "")[:160]))
    print(f"复盘是否被派生？ {events['spawned_at'] is not None}  "
          f"(应为 False：not interrupted 守卫挡住了派生)")
    interrupted_check = {
        "case": "实际中断本轮",
        "turn_exit_reason": res3.get("turn_exit_reason"),
        "interrupted": res3.get("interrupted"),
        "review_spawned": events["spawned_at"] is not None,
    }
    trace["controls"].append(interrupted_check)
    trace["controls_passed"] = (
        not disabled_check["review_spawned"]
        and disabled_check["foreground_completed"]
        and interrupted_check["interrupted"] is True
        and not interrupted_check["review_spawned"]
    )
    trace["status"] = "controls_passed" if trace["controls_passed"] else "failed"
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    if not trace["controls_passed"]:
        agent.close()
        raise AssertionError("实际中断对照未成立，请查看本次 review_trace.json。")

    # ==================================================================
    # 总结
    # ==================================================================
    banner("结论")
    print(f"1) 前台耗时={t_return - t_start:.2f}s；返回时后台存活={bool(live_at_return)}；"
          f"随后 2 秒内观察到后台存活={bool(live)}。")
    print(f"2) 线程实际启动={events['started_at'] is not None}；工具成功和文件变化见独立记录。")
    print("3) 两个不触发对照以实际派生记录为准；Memory 间隔为 0 不会关闭 Skill 检查。")

    try:
        agent.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()

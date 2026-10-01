#!/usr/bin/env python3
"""第11讲观察题：事实纠正、原生后台复盘、新会话采用，分别记录。"""
import argparse
import copy
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from observer import Observer, memory_calls, tool_calls
from scene import FIRST_TASK, FIRST_WINDOW, MIGRATION, NEXT_TASK, NEXT_WINDOW, ROLE, ROWS, SCHEMA, adoption, query

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RUNTIME = HERE / "runtime"


def completed(result):
    return bool(result.get("completed") and result.get("final_response")
                and not any(result.get(k) for k in ("failed", "error", "interrupted", "partial")))


def lesson_config(interval):
    return {"memory": {"memory_enabled": True, "user_profile_enabled": False,
                       "nudge_interval": interval, "write_approval": False},
            "skills": {"creation_nudge_interval": 0}, "compression": {"enabled": False},
            "tools": {"tool_search": {"enabled": "off"}}}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.environ.get("GLM_MODEL", "glm-5.2"))
    parser.add_argument("--base-url", default=os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    parser.add_argument("--nudge-interval", type=int, default=2, help="原生轮次间隔；0关闭自动Memory检查")
    parser.add_argument("--review-timeout", type=float, default=300, help="每次等待秒数；0表示一直等待")
    parser.add_argument("--controls", action="store_true", help="主流程结束后另做关闭检查与实际中断对照")
    parser.add_argument("--clean", metavar="RUN_ID", help="只删除指定的本讲已停止运行及全部记录")
    args = parser.parse_args()
    if args.nudge_interval < 0 or args.review_timeout < 0:
        parser.error("间隔与等待期限不能为负数")
    return args


def clean(run_id):
    if not re.fullmatch(r"run-\d{8}-\d{6}-[0-9a-f]{8}", run_id):
        raise ValueError("只接受本讲生成的完整 run 编号，不接受路径")
    run = RUNTIME / run_id
    if run.is_symlink() or run.resolve().parent != RUNTIME.resolve():
        raise ValueError("不能清理符号链接或本讲目录外的数据")
    marker = run / "RUNNING"
    if marker.exists():
        try:
            os.kill(int(marker.read_text()), 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("运行进程仍存在，不能清理")
    shutil.rmtree(run)
    print(f"已删除本讲运行及全部记录：{run_id}")


def write_report(save, summary):
    lines = ["# 第11讲本次观察", "", f"运行状态：`{summary['status']}`。", "",
             "本地构造业务数据；模型、工具与后台为实际调用。未触发、未写入、未采用都是有效观察，",
             "不需要重试到成功。新会话查对也不能单次归因于记忆。", "",
             "完整输入、回执、快照和时间见 [observation.json](observation.json)。",
             "新会话启动信息见 `sessions/fresh.json`（未执行到此步则不存在）。", ""]
    for item in summary["turns"]:
        lines.append(f"- {item['label']}：后台 `{item['review_wait']['state']}`；"
                     f"前台 memory {len(item['foreground_memory_calls'])} 次，"
                     f"后台 memory {len(item['background_memory_calls'])} 次；"
                     f"后台文件变化 `{item['background_file_changed']}`。")
    lines += ["", "新会话查询观察：", "```json", json.dumps(summary.get("new_session_behavior"), ensure_ascii=False, indent=2),
              "```", "", "请阅读完整记忆与工具顺序，区分前台保存、后台保存、已有等价记忆和现场重新发现。",
              "如有执行错误或等待未完成，先看原始状态，不将缺失记录解释成没有复盘。"]
    save("REPORT.md", "\n".join(lines) + "\n")


def run_live(args):
    key = os.environ.get("GLM_API_KEY")
    if not key:
        raise ValueError("请设置 GLM_API_KEY；本入口不读取个人 .env 或其他供应商凭据")
    src = Path(os.environ.get("HERMES_SRC", ROOT / ".deps/hermes-agent")).expanduser().resolve()
    if (src / ".env").exists():
        raise ValueError("Hermes 源码目录存在 .env；为避免上游隐式读取，请使用无凭据文件的课程依赖目录")
    revision = subprocess.check_output(["git", "-C", str(src), "rev-parse", "HEAD"], text=True).strip()
    if revision != (ROOT / "scripts/hermes-revision.txt").read_text().strip():
        raise ValueError("Hermes 提交与课程固定版本不一致；未修改依赖")
    if subprocess.check_output(["git", "-C", str(src), "status", "--porcelain", "--untracked-files=no"], text=True).strip():
        raise ValueError("Hermes 已跟踪文件有修改；请使用课程固定版本")
    run = RUNTIME / (time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    home = run / "hermes"
    home.mkdir(parents=True)
    (run / "RUNNING").write_text(str(os.getpid()))
    config = lesson_config(args.nudge_interval)
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(src))

    def redact(text):
        return text.replace(key, "[凭据已隐去]").replace(str(Path.home()), "$HOME")

    def save(name, value):
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(redact(text), encoding="utf-8")
        temporary.replace(path)

    summary = {"run_id": run.name, "hermes_revision": revision, "config": config,
               "model": args.model, "base_url": args.base_url, "turns": [], "status": "running"}
    agents, observer = [], None
    print(f"本次入口：{run / 'REPORT.md'}", flush=True)
    save("scene.json", {"deployment": MIGRATION, "logs": ROWS})
    try:
        from run_agent import AIAgent
        from agent import background_review
        from tools.registry import registry
        registry.register(name="lesson11_logs", toolset="lesson11", schema=SCHEMA,
                          handler=lambda arguments, **kw: json.dumps(query(arguments), ensure_ascii=False))
        observer = Observer(AIAgent, background_review, home, save)
        observer.install()

        def new_agent(label):
            agent = AIAgent(model=args.model, provider="glm", api_key=key, base_url=args.base_url,
                            session_id=f"lesson11-{label}-{uuid.uuid4().hex[:12]}",
                            enabled_toolsets=["lesson11", "memory"], max_iterations=6,
                            quiet_mode=True, skip_context_files=True)
            agents.append(agent)
            if agent._memory_nudge_interval != args.nudge_interval or agent._skill_nudge_interval != 0:
                raise RuntimeError("教学配置未被 Hermes 采用")
            if not {"lesson11_logs", "memory"}.issubset(agent.valid_tool_names):
                raise RuntimeError("只读日志工具或原生 memory 未就绪")
            observer.watch(agent)
            save(f"sessions/{label}.json", {"session_id": agent.session_id, "history_at_start": [],
                 "memory_at_start": observer.memory(),
                 "loaded_memory": agent._memory_store.format_for_system_prompt("memory"),
                 "model": agent.model, "nudge_interval": agent._memory_nudge_interval})
            return agent

        def turn(agent, label, message, history, allow_interrupted=False):
            agent._lesson11_turn = label
            inherited = copy.deepcopy(history)
            item = {"label": label, "session_id": agent.session_id, "user_message": message,
                    "started_at": time.time(), "memory_before": observer.memory(),
                    "counter_before": agent._turns_since_memory, "input_history": inherited}
            save(f"turns/{label}.json", item)
            result = agent.run_conversation(user_message=message, system_message=ROLE,
                                            conversation_history=history)
            item.update(returned_at=time.time(), result=result,
                        counter_after=agent._turns_since_memory,
                        calls=tool_calls(result.get("messages", []), inherited))
            review = observer.reviews.get(label)
            item["review_alive_at_return"] = bool(observer.handles.get(label) and observer.handles[label].is_alive())
            item["memory_after_foreground"] = review["memory_at_spawn"] if review else observer.memory()
            save(f"turns/{label}.json", item)
            print(redact(f"\n{label} 前台：{result.get('final_response', '')}"), flush=True)
            item["review_wait"] = observer.wait(label, args.review_timeout)
            item["memory_after_wait"] = observer.memory()
            item["foreground_memory_calls"] = memory_calls(item["calls"])
            item["background_memory_calls"] = memory_calls((review or {}).get("calls", []))
            item["background_file_changed"] = (review.get("memory_before") != review.get("memory_after")
                                                   if review and "memory_after" in review else None)
            summary["turns"].append(item)
            save(f"turns/{label}.json", item)
            save("observation.json", summary)
            for filename in ("MEMORY.md", "USER.md"):
                save(f"memory/{label}-{filename}.diff", "".join(difflib.unified_diff(
                    item["memory_before"][filename].splitlines(True),
                    item["memory_after_wait"][filename].splitlines(True), fromfile="本轮前", tofile="等待后")))
            print(f"后台：{item['review_wait']['state']}；前台 memory 调用 {len(item['foreground_memory_calls'])}；"
                  f"后台 memory 调用 {len(item['background_memory_calls'])}；文件变化 {item['background_file_changed']}", flush=True)
            if not completed(result) and not (allow_interrupted and result.get("interrupted")):
                raise RuntimeError(f"{label} 前台未正常完成；不是学习失败，详见原始返回")
            return item

        original = new_agent("original")
        first = turn(original, "01-task", FIRST_TASK, [])
        if first["review_wait"]["state"] == "pending":
            summary["status"] = "pending_at_exit"
            return 0
        summary["first_task_behavior"] = adoption(first["calls"], FIRST_WINDOW)
        correction = "补充已核实的正式迁移说明：\n" + json.dumps(MIGRATION, ensure_ascii=False)
        correction += "\n请据此复核刚才同一告警时段的日志并说明依据；如果已查对，也按实际结果说明。"
        second = turn(original, "02-correction", correction, first["result"].get("messages", []))
        if second["review_wait"]["state"] == "pending":
            summary["status"] = "pending_at_exit"
            return 0
        summary["corrected_task_behavior"] = adoption(second["calls"], FIRST_WINDOW)
        fresh = new_agent("fresh")
        third = turn(fresh, "03-fresh", NEXT_TASK, [])
        summary["new_session_behavior"] = adoption(third["calls"], NEXT_WINDOW)
        summary["status"] = "pending_at_exit" if third["review_wait"]["state"] == "pending" else "observed"
        if args.controls and summary["status"] != "pending_at_exit":
            control = new_agent("controls")
            control._memory_nudge_interval = control._skill_nudge_interval = 0
            off = turn(control, "control-disabled", NEXT_TASK, [])
            control._memory_nudge_interval = 1
            timer = threading.Timer(0.2, lambda: control.interrupt("观察实际中断", hard_cancel=True))
            timer.start()
            try:
                interrupted = turn(control, "control-interrupted", NEXT_TASK, [], allow_interrupted=True)
            finally:
                timer.cancel()
                timer.join()
            summary["controls"] = {"disabled_spawned": off["review_wait"]["state"] != "not_triggered",
                "actually_interrupted": interrupted["result"].get("interrupted", False),
                "interrupted_spawned": interrupted["review_wait"]["state"] != "not_triggered"}
            if interrupted["review_wait"]["state"] == "pending":
                summary["status"] = "pending_at_exit"
        return 0
    except BaseException as exc:
        summary.update(status="execution_failed", error=repr(exc))
        print(redact(str(exc)), file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        # 超时不继续新任务，不把 daemon 被进程退出中止说成正常完成。
        pending = []
        if observer:
            pending = [k for k in observer.reviews if observer.wait(k, 0.01)["state"] == "pending"]
            if pending:
                summary["pending_reviews"] = pending
            else:
                observer.restore()
                for agent in agents:
                    agent.close()
        save("observation.json", summary)
        write_report(save, summary)
        # 仍有 daemon 时保留 PID 标记；下次清理会核对该进程已退出。
        if not pending:
            (run / "RUNNING").unlink(missing_ok=True)
        print(f"记录保留在：{run / 'REPORT.md'}", flush=True)


def main():
    args = parse_args()
    if args.clean:
        clean(args.clean)
        return 0
    return run_live(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

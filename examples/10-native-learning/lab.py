#!/usr/bin/env python3
"""第 10 讲独立观察。原生 Hermes CLI 执行任务，本地只准备和读取观察材料。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME = HERE / "runtime"
SCHEMA = "lesson10-observation-v1"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snapshot(home):
    files = {str(path.relative_to(home)): path.read_text(encoding="utf-8")
             for directory in ("memories", "skills")
             for path in sorted((home / directory).rglob("*.md")) if path.is_file()}
    return {"files": files, "sha256": {name: hashlib.sha256(text.encode()).hexdigest()
                                      for name, text in files.items()}}


def prepare(label="task"):
    run = RUNTIME / (time.strftime("%Y%m%d-%H%M%S-") + label + "-" + uuid.uuid4().hex[:8])
    home = run / "hermes"
    workspace = run / "workspace"
    home.mkdir(parents=True)
    workspace.mkdir()
    config = {
        "model": {"default": os.environ.get("GLM_MODEL", "glm-5.2"), "provider": "glm",
                  "base_url": os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")},
        "memory": {"memory_enabled": True, "user_profile_enabled": True},
        "terminal": {"backend": "local", "cwd": str(workspace)},
    }
    save(home / "config.yaml", config)
    save(run / "run.json", {"schema": SCHEMA, "run_id": run.name, "config": config})
    save(run / "initial.json", snapshot(home))
    return run


def selected(identifier):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("请使用 prepare 打印的运行编号")
    run = RUNTIME / identifier
    if run.is_symlink() or run.resolve().parent != RUNTIME.resolve():
        raise ValueError("运行目录不属于本讲")
    if any((run / name).is_symlink() for name in ("hermes", "workspace")):
        raise ValueError("教学 Home 和工作目录须保留在本次运行内")
    if json.loads((run / "run.json").read_text())["schema"] != SCHEMA:
        raise ValueError("运行标记不属于本讲")
    return run


def source():
    location = Path(os.environ.get("HERMES_SRC", ROOT / ".deps/hermes-agent")).resolve()
    expected = (ROOT / "scripts/hermes-revision.txt").read_text().strip()
    actual = subprocess.check_output(["git", "-C", str(location), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(location), "status", "--porcelain", "--untracked-files=no"], text=True)
    if actual != expected or dirty.strip() or (location / ".env").exists():
        raise ValueError("请使用没有 .env 的课程固定 Hermes 提交")
    return location


def inspect(run):
    current = snapshot(run / "hermes")
    before = json.loads((run / "initial.json").read_text())
    changed = [name for name, digest in current["sha256"].items() if before["sha256"].get(name) != digest]
    removed = sorted(set(before["files"]) - set(current["files"]))
    report = {"run_id": run.name, "changed_files": changed, "removed_files": removed, **current}
    save(run / "inspection.json", report)
    for name in changed:
        print(f"\n{name}\n{current['files'][name]}")
    if not changed:
        print("尚未观察到 Memory 或 Skill 文件新增、修订。")
    print(f"完整观察记录 {run / 'inspection.json'}")
    print(f"原生会话数据库 {run / 'hermes/state.db'}")


def chat(run, live):
    if not live:
        raise ValueError("chat 会调用模型，请阅读本讲说明并显式加 --live")
    if not os.environ.get("GLM_API_KEY", "").strip():
        raise ValueError("请在当前终端设置 GLM_API_KEY")
    native = source()
    record = run / "chats" / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    save(record / "before.json", snapshot(run / "hermes"))
    environment = {**os.environ, "HERMES_HOME": str(run / "hermes"),
                   "TERMINAL_CWD": str(run / "workspace"), "TERMINAL_ENV": "local",
                   "PYTHONDONTWRITEBYTECODE": "1"}
    environment["PYTHONPATH"] = str(native)
    # CLI 继续使用原生会话和复盘；本讲显式接收当前终端的模型凭据。
    entry = ("import hermes_cli.env_loader as loader; "
             "loader.load_hermes_dotenv = lambda **kwargs: []; "
             "from hermes_cli.main import main; main()")
    command = [sys.executable, "-B", "-c", entry, "chat", "--provider", "glm"]
    print("把小任务交给 Hermes。等后台提示和文件变化后，用 /quit 退出，再 inspect。", flush=True)
    receipt = {"returncode": None}
    try:
        result = subprocess.run(command, cwd=run / "workspace", env=environment)
        receipt["returncode"] = result.returncode
        return result.returncode
    finally:
        save(record / "after.json", snapshot(run / "hermes"))
        save(record / "exit.json", receipt)


def native_check():
    native = source()
    run = prepare("native-check")
    home = run / "hermes"
    os.environ["HERMES_HOME"] = str(home)
    os.environ["TERMINAL_CWD"] = str(run / "workspace")
    sys.path.insert(0, str(native))

    def prohibit_network(event, args):
        if event == "socket.connect":
            raise RuntimeError("本地检查禁止网络连接")

    sys.addaudithook(prohibit_network)
    import hermes_cli.env_loader as loader
    loader.load_hermes_dotenv = lambda **kwargs: []
    from run_agent import AIAgent
    from hermes_state import SessionDB
    database = SessionDB(home / "state.db")
    agent = None
    try:
        agent = AIAgent(model="glm-5.2", provider="glm", api_key="unused-lesson10-native-check",
                        base_url="https://open.bigmodel.cn/api/paas/v4",
                        session_id="lesson10-check-" + uuid.uuid4().hex, session_db=database,
                        enabled_toolsets=["terminal", "memory", "skills"], quiet_mode=True,
                        skip_context_files=True, load_soul_identity=False)
        required = {"terminal", "memory", "skills_list", "skill_view", "skill_manage"}
        if not required.issubset(agent.valid_tool_names):
            raise RuntimeError("本讲需要的原生工具没有加载")
        result = {"model_requests": 0, "native_initialization": True,
                  "memory_review_interval": agent._memory_nudge_interval,
                  "skill_review_interval": agent._skill_nudge_interval,
                  "tools": sorted(required)}
        save(run / "check.json", result)
    finally:
        if agent is not None:
            agent.close()
        database.close()
    print(f"原生工具、会话库和学习配置已就绪，模型请求为零。记录 {run / 'check.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("check", help="只初始化原生接口，禁止联网")
    prepare_parser = commands.add_parser("prepare", help="建立本讲自己的教学 Home 与工作目录")
    prepare_parser.add_argument("--id-only", action="store_true", help="只输出运行编号，便于当前终端继续操作")
    for name in ("chat", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--run", required=True, help="prepare 打印的运行编号")
        if name == "chat":
            command.add_argument("--live", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "check":
            native_check()
        elif args.action == "prepare":
            run = prepare()
            print(run.name if args.id_only else f"运行编号 {run.name}\n工作目录 {run / 'workspace'}")
        elif args.action == "inspect":
            inspect(selected(args.run))
        else:
            return chat(selected(args.run), args.live)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"本讲操作未完成 {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

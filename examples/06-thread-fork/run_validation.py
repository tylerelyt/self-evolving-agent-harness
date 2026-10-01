"""06 讲整本验证；离线回放与真实模型检查分别记录。"""
import argparse
import ast
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter

QUESTION_FORBIDDEN = ["fork", "lastTurnId", "forkedFromId", "thread/fork", "ForkBoundary", "Resume", "PreparedFork"]
STAGES = ["Agent 为什么一直在排查另一套系统？", "Fork 怎样为新的事故建立独立的 Thread？",
          "新 Thread 怎样接回模块背景，重新调查当前告警？"]
INTRO_SHA256 = "d2e32d8a06d3b3eebd18bc89ffcee6020217f2f08599802b00cc87e523866e0b"


class ReadingTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = 0

    def handle_starttag(self, tag, attrs):
        if tag == "div" and "act-result" in dict(attrs).get("class", "").split():
            self.tables += 1


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_source(notebook):
    code = [c for c in notebook.cells if c.cell_type == "code"]
    text = "\n".join(c.source for c in notebook.cells)
    question = notebook.cells[0].source.split("## 题面", 1)[1].split("## 参考答案", 1)[0]
    hits = [word for word in QUESTION_FORBIDDEN if word.lower() in question.lower()]
    lengths = [len(c.source.splitlines()) for c in code]
    checks = {
        "十二格结构与七个代码格": [c.cell_type for c in notebook.cells] ==
            ["markdown", "markdown", "code", "markdown", "code", "code", "markdown", "code", "code", "markdown", "code", "code"],
        "标题定位题面答案逐字符合任务书": hashlib.sha256(notebook.cells[0].source.encode()).hexdigest() == INTRO_SHA256,
        "准备格七行": len(notebook.cells[2].source.splitlines()) == 7,
        "代码格保留完整调用且不超过三十二行": all(3 <= length <= 32 for length in lengths),
        "代码格没有断言": not any("assert" in c.source or any(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(c.source))) for c in code),
        "源notebook无执行输出": all(not c.outputs and c.execution_count is None for c in code),
        "题面没有禁用机制词": not hits,
        "引导对齐定稿三个小节": all(notebook.cells[i].source.splitlines()[0] == "## " + stage for i, stage in zip((3, 6, 9), STAGES)),
        "每阶段约三行引导": all(len([line for line in notebook.cells[i].source.splitlines()[1:] if line.strip()]) == 3 for i in (3, 6, 9)),
        "没有元指令词": not any(word in text for word in ("必读", "必跑", "可折叠", "可选", "这只是初始化")),
        "结尾直接收尾": not any(word in "\n".join(c.source for c in notebook.cells[9:]) for word in ("小结", "课后题", "继续观察", "清理")),
        "全文没有禁用分节字": "幕" not in text,
        "核心分岔直接写在代码格": "thread_fork(parent.id" in notebook.cells[8].source and '"lastTurnId"' in notebook.cells[8].source,
    }
    return {"cell_count": len(notebook.cells), "code_count": len(code), "code_lines": lengths,
            "question_forbidden_hits": hits, "checks": checks}


OFFLINE_GUARD = '''import importlib.abc, socket, subprocess, sys
_offline_guard = {"blocked_imports": [], "blocked_network": [], "blocked_processes": []}
class _NoModelImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"notebook_support", "openai_codex", "litellm"}:
            _offline_guard["blocked_imports"].append(fullname)
            raise RuntimeError("离线禁止加载模型设施")
sys.meta_path.insert(0, _NoModelImports())
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
def _local_address(address):
    return isinstance(address, str) or (isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"})
def _local_connect(sock, address):
    if not _local_address(address):
        _offline_guard["blocked_network"].append(str(address))
        raise RuntimeError("离线禁止外部连接")
    return _connect(sock, address)
def _local_connect_ex(sock, address):
    if not _local_address(address):
        _offline_guard["blocked_network"].append(str(address))
        raise RuntimeError("离线禁止外部连接")
    return _connect_ex(sock, address)
socket.socket.connect, socket.socket.connect_ex = _local_connect, _local_connect_ex
_popen = subprocess.Popen
def _material_process(args, *positional, **kwargs):
    if not (isinstance(args, list) and len(args) >= 3 and args[:2] == [sys.executable, "-B"]
            and args[2] in {"query.py", "replay_retry.py"} and not kwargs.get("shell")):
        _offline_guard["blocked_processes"].append("非材料查询进程")
        raise RuntimeError("离线只允许本地材料查询与内存重放")
    return _popen(args, *positional, **kwargs)
subprocess.Popen = _material_process
'''


def run(mode):
    directory = Path(__file__).resolve().parent
    root = directory.parents[1]
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("请选择 Python 3.12 执行验证。")
    if mode == "live" and not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("实际模型验证需要 GLM_API_KEY；本次额度不足时请使用 offline。")
    run_id = "nb06-v1-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True, exist_ok=False)
    source = directory / "workshop.ipynb"
    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    files = [source, Path(__file__).resolve(), directory / "README.md",
             root / "examples/notebook06_setup.py", root / "examples/notebook06_story.py",
             root / "examples/notebook_support.py",
             *sorted(p for p in (directory / "materials").rglob("*") if p.is_file())]
    hashes = {str(p.relative_to(root)): digest(p) for p in files}
    record = {"mode": mode, "run_id": run_id, "status": "running", "hashes": hashes,
              "source_checks": inspect_source(notebook), "cells": [],
              "live_executed": mode == "live", "coverage": "材料回放" if mode == "offline" else "真实模型路径"}
    secret_values = [v for k, v in os.environ.items() if len(v) > 8 and
                     any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]

    def clean(text):
        for value in secret_values:
            text = text.replace(value, "<凭证已隐藏>")
        return text.replace(str(root), "<项目根目录>").replace(str(Path.home()), "<用户目录>")

    def save():
        content = clean(json.dumps(record, ensure_ascii=False, indent=2)) + "\n"
        for folder in (out, directory / "validation"):
            for name in ("result.json", "status.json"):
                (folder / name).write_text(content, encoding="utf-8")

    client = NotebookClient(notebook, timeout=1200, kernel_name="python3", allow_errors=False,
                            resources={"metadata": {"path": str(directory)}})
    client.create_kernel_manager()
    client.km.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE="1", LAB_NOTEBOOK06_MODE=mode, LAB_VALIDATION_RUN_ID=run_id)
    for name in list(env):
        if any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and (mode == "offline" or name != "GLM_API_KEY"):
            env.pop(name, None)
    for name, folder in (("IPYTHONDIR", "ipython"), ("JUPYTER_RUNTIME_DIR", "jupyter")):
        (out / folder).mkdir()
        env[name] = str(out / folder)

    def extra(code):
        cell = nbformat.v4.new_code_cell(code)
        notebook.cells.append(cell)
        try:
            client.execute_cell(cell, len(notebook.cells) - 1, store_history=False)
        finally:
            notebook.cells.pop()
        return "".join(o.get("text", "") for o in cell.outputs)

    runtime = None
    started = time.monotonic()
    save()
    try:
        if not all(record["source_checks"]["checks"].values()):
            raise RuntimeError("源 notebook 检查失败。")
        with client.setup_kernel(env=env):
            extra('''import getpass, os
_key_inputs = 0
_test_key = os.environ.pop("GLM_API_KEY", "")
def _key_input(prompt):
    global _key_inputs
    _key_inputs += 1
    return _test_key
getpass.getpass = _key_input
get_ipython().kernel.getpass = _key_input
''')
            if mode == "offline":
                extra(OFFLINE_GUARD)
            try:
                for index, cell in enumerate(notebook.cells):
                    if cell.cell_type != "code":
                        continue
                    print(f"开始 cell{index}", flush=True)
                    start = time.monotonic()
                    status = "failed"
                    try:
                        client.execute_cell(cell, index, execution_count=client.code_cells_executed + 1)
                        status = "finished"
                    finally:
                        record["cells"].append({"index": index, "seconds": round(time.monotonic() - start, 3), "status": status})
                        save()
                    if index == 2:
                        runtime = Path(extra("print(story.runtime)").strip())
                        record["runtime"] = str(runtime.relative_to(root))
                    print(f"结束 cell{index}", flush=True)
            finally:
                extra('if "story" in globals(): story.close()')
                record["key_inputs"] = int(extra("print(_key_inputs)").strip())
                if mode == "offline":
                    record["offline_guard"] = json.loads(extra("import json; print(json.dumps(_offline_guard))"))
        if runtime is None:
            raise RuntimeError("准备格没有创建本次运行目录。")
        def read(name):
            return json.loads((runtime / name).read_text(encoding="utf-8"))
        record["stages"] = read("stages.json")
        record["request_count"] = len(list(runtime.glob("request-*.json")))
        turns = [json.loads(p.read_text()) for p in sorted(runtime.glob("turn-[0-9]*.json"))]
        record["turn_count"] = len(turns)
        cleanup = read("cleanup.json")
        record["cleanup"] = cleanup
        checks = dict(record["source_checks"]["checks"])
        checks.update({"执行期间源文件未变": all(digest(root / name) == value for name, value in hashes.items()),
                       "七个代码格均执行": [c["index"] for c in record["cells"] if c["status"] == "finished"] == [2, 4, 5, 7, 8, 10, 11],
                       "三阶段均保存记录": [s["stage"] for s in record["stages"]] == STAGES,
                       "连接已关闭": cleanup["closed"] and not cleanup["errors"],
                       "key输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0)})
        checks.update(read("current-investigation.json")["checks"])
        boundary = read("fork-boundary.json")
        checks.update({"选择只含背景的材料前缀": boundary["material_boundary"] == 1 and len(boundary["selected_prefix"]) == 1
                                                     and boundary["selected_prefix"][0]["scope"] == "模块背景",
                       "材料前缀排除旧事故观察": all("OLD-" not in row["text"] for row in boundary["selected_prefix"])})
        if mode == "offline":
            checks.update({"没有模型请求或代理进程": not turns and record["request_count"] == 0 and not (runtime / "adapter.log").exists()
                                                    and not cleanup["started_connections"] and not cleanup["process_returncodes"],
                           "未触及模型导入外部连接或无关进程": not any(record["offline_guard"].values()),
                           "没有伪造Thread与模型报告": boundary["parent_id"] is None and boundary["branch_id"] is None
                                                       and read("current-investigation.json")["model_report"] is None,
                           "两套目录入口不同": all(p["exit_code"] == 2 for p in read("offline-wrong-entry-probes.json"))})
        else:
            record["observations"] = read("wrong-thread.json")["checks"]
            checks.update(boundary["checks"])
            checks.update({"九轮均完成": len(turns) == 9 and all(t["status"] == "completed" for t in turns),
                           "模型代理及分岔客户端已释放": set(cleanup["process_returncodes"]) == {"model", "fork", "proxy"}
                               and all(code is not None for code in cleanup["process_returncodes"].values()) and not any(cleanup["ports_open"].values())})
        record["checks"] = checks
        record["status"] = "passed" if all(value is True for value in checks.values()) else "failed"
    except BaseException as exc:
        record.update(status="failed", error_type=type(exc).__name__, error=clean(str(exc)))
    finally:
        record["seconds"] = round(time.monotonic() - started, 3)
        notebook.metadata["validation"] = {key: record.get(key) for key in ("mode", "run_id", "status")}
        (out / "executed.ipynb").write_text(clean(nbformat.writes(notebook)), encoding="utf-8")
        html, _ = HTMLExporter().from_notebook_node(nbformat.read(out / "executed.ipynb", as_version=4))
        # 本讲没有公式或交互控件，阅读页保留内嵌样式，删除会联网加载的脚本。
        html = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", html, flags=re.S | re.I)
        html = html.replace("<title>Notebook</title>", "<title>Lab 06｜调查边界与离线阅读</title>")
        parser = ReadingTables()
        parser.feed(html)
        record["reading_checks"] = {"三阶段四张过程表格": parser.tables == 4,
                                    "表格左对齐": "text-align:left" in html,
                                    "阅读页无外部脚本": not re.search(r"<script\b", html, re.I),
                                    "全文无禁用分节字": "幕" not in html}
        if not all(record["reading_checks"].values()):
            record["status"] = "failed"
        (out / "reading.html").write_text(html, encoding="utf-8")
        save()
        (directory / "validation" / (mode + "-latest.json")).write_text(json.dumps(
            {"run_id": run_id, "status": record["status"], "reading": "runs/" + run_id + "/reading.html"}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: record.get(key) for key in ("status", "mode", "run_id", "seconds", "key_inputs", "turn_count", "request_count")}, ensure_ascii=False), flush=True)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    args = parser.parse_args()
    sys.exit(0 if run(args.mode)["status"] == "passed" else 1)

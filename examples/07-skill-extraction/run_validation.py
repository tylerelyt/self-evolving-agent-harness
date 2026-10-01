"""07 讲整本执行；分别核验材料回放与真实模型路径。"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(mode):
    directory = Path(__file__).resolve().parent
    root = directory.parents[1]
    source = directory / "workshop.ipynb"
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("请选择 Python 3.12 执行验证。")
    if mode == "live" and not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("实际模型核验需要 GLM_API_KEY 环境变量。")
    run_id = "nb07-v9-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S")
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True)
    files = [source, Path(__file__).resolve(), root / "examples/notebook07_setup.py", root / "examples/notebook07_story.py",
             root / "examples/notebook_support.py", *sorted((directory / "materials").glob("*")),
             *sorted(p for p in (directory / "skill_creator").rglob("*") if p.is_file() and "__pycache__" not in p.parts)]
    hashes = {str(p.relative_to(root)): digest(p) for p in files}
    record = {"mode": mode, "run_id": run_id, "status": "running", "hashes": hashes, "cells": []}
    secret_values = [v for k, v in os.environ.items() if len(v) > 8 and
                     any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]

    def clean(text):
        for value in secret_values:
            text = text.replace(value, "<凭证已隐藏>")
        return text.replace(str(root), "<项目根目录>").replace(str(Path.home()), "<用户目录>")

    def save():
        content = clean(json.dumps(record, ensure_ascii=False, indent=2)) + "\n"
        (out / "result.json").write_text(content)
        for name in ("result.json", "status.json"):
            (directory / "validation" / name).write_text(content)

    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    client = NotebookClient(notebook, timeout=1200, kernel_name="python3", allow_errors=False,
                            resources={"metadata": {"path": str(directory)}})
    client.create_kernel_manager()
    client.km.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    env = os.environ.copy()
    env.pop("BIGMODEL_API_KEY", None)
    if mode == "offline":
        env.pop("GLM_API_KEY", None)
        env["LAB_VALIDATION_RUN_ID"] = run_id
    else:
        env.pop("LAB_VALIDATION_RUN_ID", None)
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
        return "".join(o.get("text", "") for o in cell.get("outputs", []))

    runtime = None
    start = time.monotonic()
    save()
    try:
        with client.setup_kernel(env=env):
            # 从环境取验证凭证，然后清空变量，真实经过 notebook 的 getpass 分支。
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
            try:
                for index, cell in enumerate(notebook.cells):
                    if cell.cell_type != "code":
                        continue
                    print(f"开始 cell{index}", flush=True)
                    cell_start = time.monotonic()
                    try:
                        client.execute_cell(cell, index, execution_count=client.code_cells_executed + 1)
                    finally:
                        errors = [o for o in cell.get("outputs", []) if o.output_type == "error"]
                        record["cells"].append({"index": index, "seconds": round(time.monotonic() - cell_start, 3),
                                                "status": "failed" if errors else "finished"})
                        save()
                    if index == 2:
                        runtime = Path(extra("print(story.runtime)").strip())
                        record["runtime"] = str(runtime.relative_to(root))
                        extra('''if story.lab:
    import socket
    _lab = story.lab
    _proxy = _lab.proxy
    _model_proc = getattr(_lab.codex._client, "_proc", None)
    _ports = [int(_proxy.args[_proxy.args.index("--port")+1]), _lab.observer.server_address[1]]
    _close = _lab.close
    def _checked_close():
        try:
            return _close()
        finally:
            _open = {}
            for _port in _ports:
                with socket.socket() as _sock:
                    _sock.settimeout(1)
                    _open[str(_port)] = _sock.connect_ex(("127.0.0.1", _port)) == 0
            _lab._write_json("process-cleanup.json", {"proxy_returncode": _proxy.poll(),
                "model_returncode": _model_proc.poll() if _model_proc else None,
                "ports_open": _open, "errors": _lab._cleanup_errors})
    _lab.close = _checked_close
''')
                    print(f"结束 cell{index}，{record['cells'][-1]['seconds']} 秒", flush=True)
            finally:
                extra('if "story" in globals(): story.close()')
                record["key_inputs"] = int(extra("print(_key_inputs)").strip())
        record["status"] = "executed"
        if runtime is None:
            raise RuntimeError("本次准备格没有创建运行目录。")
        turns = [json.loads(p.read_text()) for p in sorted(runtime.glob("turn-[0-9]*.json"))]
        record["turn_count"] = len(turns)
        record["request_count"] = len(list(runtime.glob("request-*.json")))
        record["acts"] = json.loads((runtime / "acts.json").read_text())
        skill = runtime / "work/legacy-payment/.agents/skills/legacy-payment-oncall"
        checks = {
            "执行期间版本未变": all(digest(root / name) == value for name, value in hashes.items()),
            "七步方法与定稿展示版本一致": (skill / "SKILL.md").read_text() == (directory / "materials/reviewed-SKILL.md").read_text(),
            "第一版没有资源目录": all(not (skill / name).exists() for name in ("scripts", "references", "assets")),
            "初稿与修改后结构均合格": all(json.loads((runtime / name).read_text())["exit_code"] == 0
                for name in ("first-draft-validation.json", "reviewed-validation.json")),
            "没有修改旧调查记录": digest(runtime / "work/history/old-thread.md") == digest(directory / "materials/old-thread.md"),
            "没有修改新事故材料": digest(runtime / "work/legacy-payment/current/current.json") == digest(directory / "materials/current.json"),
            "代码格没有断言语句": not any(isinstance(node, ast.Assert) for c in notebook.cells if c.cell_type == "code" for node in ast.walk(ast.parse(c.source))),
            "代码格保留完整调用且不超过三十二行": all(3 <= len(c.source.splitlines()) <= 32 for c in notebook.cells if c.cell_type == "code"),
            "连接已关闭": json.loads((runtime / "cleanup.json").read_text())["closed"],
            "输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0),
        }
        if mode == "offline":
            checks["没有模型请求或代理进程"] = not turns and record["request_count"] == 0 and not (runtime / "adapter.log").exists()
        else:
            reads = json.loads((runtime / "new-alert-reads.json").read_text())["queried"]
            fresh = json.loads((runtime / "fresh-thread.json").read_text())
            checks.update({
                "四个入口均有成功查询回执": all(reads.values()),
                "新调查使用独立Thread": fresh["thread_id"] != fresh["previous_thread_id"],
                "模型轮均完成": len(turns) == 3 and all(t["status"] == "completed" for t in turns),
                "模型提炼文件结构合格": json.loads((runtime / "model-draft-validation.json").read_text())["exit_code"] == 0,
                "模型提炼内容单独留存": (runtime / "model-draft-SKILL.md").is_file(),
            })
            # 新调查自己的首次模型请求不含旧事故现场，后续工具也没有读取旧 Thread。
            new_start = sum(a["requests"] for a in record["acts"][:2])
            new_requests = [json.loads(p.read_text()) for p in sorted(runtime.glob("request-*.json"))][new_start:]
            checks["新Thread没有携入或重读旧现场"] = all("pay-old-001" not in json.dumps(r, ensure_ascii=False) and "history/old-thread.md" not in json.dumps(r, ensure_ascii=False) for r in new_requests)
            cleanup = json.loads((runtime / "process-cleanup.json").read_text())
            record["process_cleanup"] = cleanup
            checks["进程和端口已释放"] = cleanup["proxy_returncode"] is not None and cleanup["model_returncode"] is not None and not any(cleanup["ports_open"].values()) and not cleanup["errors"]
        record["checks"] = checks
        record["status"] = "passed" if all(checks.values()) else "failed"
    except BaseException as exc:
        record.update(status="failed", error_type=type(exc).__name__, error=clean(str(exc)))
    finally:
        record["seconds"] = round(time.monotonic() - start, 3)
        notebook.metadata["validation"] = {k: record.get(k) for k in ("mode", "run_id", "status")}
        (out / "executed.ipynb").write_text(clean(nbformat.writes(notebook)))
        exporter = HTMLExporter()
        html, _ = exporter.from_notebook_node(nbformat.read(out / "executed.ipynb", as_version=4))
        (out / "reading.html").write_text(html, encoding="utf-8")
        save()
        print(json.dumps({k: record.get(k) for k in ("status", "mode", "seconds", "key_inputs", "turn_count", "request_count")}, ensure_ascii=False), flush=True)
        (directory / "validation" / (mode + "-latest.json")).write_text(json.dumps({"run_id": run_id, "status": record["status"]}, ensure_ascii=False))
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    args = parser.parse_args()
    sys.exit(0 if run(args.mode)["status"] == "passed" else 1)

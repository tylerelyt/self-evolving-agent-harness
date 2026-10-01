"""01 讲整本执行；分别核验材料回放与真实模型路径。"""
import argparse
import ast
import hashlib
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


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(folder):
    return {str(p.relative_to(folder)): digest(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def inspect_source(notebook):
    code = [c for c in notebook.cells if c.cell_type == "code"]
    lengths = [len(c.source.splitlines()) for c in code]
    assertions = sum(isinstance(node, ast.Assert) for c in code for node in ast.walk(ast.parse(c.source)))
    text = "\n".join(c.source for c in notebook.cells)
    question = notebook.cells[0].source.split("## 题面", 1)[1].split("## 参考答案", 1)[0]
    hits = [word for word in ("Thread", "Turn", "call_id", "工具回执", "Agent Loop", "会话", "请求记录") if word in question]
    tail = "\n".join(c.source for c in notebook.cells[9:])
    checks = {
        "十二格结构与七个代码格": len(notebook.cells) == 12 and len(code) == 7 and
            [c.cell_type for c in notebook.cells] == ["markdown", "markdown", "code", "markdown", "code", "code", "markdown", "code", "code", "markdown", "code", "code"],
        "代码格保留完整调用且不超过三十二行": all(3 <= n <= 32 for n in lengths),
        "代码格没有断言语句": assertions == 0 and not any("assert" in c.source for c in code),
        "源notebook没有执行输出": all(not c.outputs and c.execution_count is None for c in code),
        "题面没有机制词": not hits,
        "结尾没有额外分节": not any(word in tail for word in ("小结", "课后题", "继续观察", "清理")),
        "没有元指令词": not any(word in text for word in ("必读", "必跑", "可折叠", "账号费用", "这只是初始化")),
    }
    result = {"cell_count": len(notebook.cells), "code_count": len(code), "code_lines": lengths,
              "assert_count": assertions, "question_forbidden_hits": hits, "checks": checks}
    print("源 notebook 自检：" + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def run(mode):
    directory = Path(__file__).resolve().parent
    root = directory.parents[1]
    source = directory / "workshop.ipynb"
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("请选择 Python 3.12 执行验证。")
    if mode == "live" and not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("实际模型核验需要 GLM_API_KEY 环境变量。")
    run_id = "nb01-v1-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True)
    files = [source, Path(__file__).resolve(), root / "examples/notebook01_setup.py", root / "examples/notebook01_story.py",
             root / "examples/notebook_support.py", *sorted((directory / "materials").glob("*"))]
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
        for name in ("result.json", "status.json"):
            (out / name).write_text(content)
        for name in ("result.json", "status.json"):
            (directory / "validation" / name).write_text(content)

    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    shape = inspect_source(notebook)
    record["source_checks"] = shape
    save()
    if not all(shape["checks"].values()):
        raise RuntimeError("源 notebook 结构检查失败，请先修正。")
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
        run_info = json.loads((runtime / "run.json").read_text())
        loop_marker = run_info["loop_marker"]
        receipt_record = json.loads((runtime / "receipts.json").read_text())
        report_checks = json.loads((runtime / "report-checks.json").read_text())
        source_notebook = nbformat.read(source, as_version=4)
        shape = inspect_source(source_notebook)
        record["source_checks"] = shape
        checks = {
            "执行期间版本未变": all(digest(root / name) == value for name, value in hashes.items()),
            **shape["checks"],
            "报告已生成且非空": report_checks["report_nonempty"],
            "输入目录前后未变": report_checks["inputs_unchanged"] and
                json.loads((runtime / "inputs-before.json").read_text()) ==
                json.loads((runtime / "inputs-after.json").read_text()) ==
                snapshot(runtime / "work/inputs"),
            "三幕留痕完整": [a["act"] for a in record["acts"]] == ["第一幕", "第二幕", "第三幕"],
            "连接已关闭": json.loads((runtime / "cleanup.json").read_text())["closed"],
            "输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0),
        }
        if mode == "offline":
            checks.update({"没有模型请求或适配器日志": not turns and record["request_count"] == 0 and not (runtime / "adapter.log").exists(),
                           "离线没有模拟回执": not receipt_record["receipts"] and not receipt_record["marker_requests"]})
        else:
            sys.path.insert(0, str(root / "examples"))
            from notebook_support import command_receipts, tool_output_contains
            requests = [json.loads(p.read_text()) for p in sorted(runtime.glob("request-*.json"))]
            receipts = command_receipts(requests)
            report = (runtime / "work/outputs/incident-report.md").read_text(encoding="utf-8")
            checks.update({
                "一次调查包含至少两次模型请求": len(requests) >= 2,
                "首个请求不含日志随机编号": bool(requests) and loop_marker not in json.dumps(requests[0], ensure_ascii=False),
                "成功命令回执包含随机编号": any(r["exit_code"] == 0 and loop_marker in r["output"] for r in receipts),
                "随机编号经回执进入后续请求": any(tool_output_contains(r, loop_marker) for r in requests[1:]),
                "调用与回执配对结果一致": bool(receipts) and receipts == receipt_record["receipts"],
                "调查正常结束": len(turns) == 1 and all(t["status"] == "completed" for t in turns),
                "报告引用本次随机编号": loop_marker in report,
            })
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

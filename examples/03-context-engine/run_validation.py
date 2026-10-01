"""03 讲整本执行；分别核验材料回放与真实模型路径。"""
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
    hits = [word for word in ("Prompt", "input", "tools", "instructions", "base_instructions", "run_turn", "sampling_request", "Context", "build_prompt") if word in question]
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
              "assert_count": assertions, "question_forbidden_hits": hits, "question_forbidden_count": len(hits), "checks": checks}
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
    run_id = "nb03-v1-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True)
    files = [source, Path(__file__).resolve(), root / "examples/notebook03_setup.py", root / "examples/notebook03_story.py",
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
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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
        record["request_count"] = len(list(runtime.glob("request-[0-9]*.json")))
        record["acts"] = json.loads((runtime / "acts.json").read_text())
        run_info = json.loads((runtime / "run.json").read_text())
        marker = run_info["marker"]
        comparison = json.loads((runtime / "comparison.json").read_text())
        composition = json.loads((runtime / "composition.json").read_text())
        assembly = json.loads((runtime / "request-assembly.json").read_text())
        followup = json.loads((runtime / "followup.json").read_text())
        source_notebook = nbformat.read(source, as_version=4)
        shape = inspect_source(source_notebook)
        record["source_checks"] = shape
        stages = ["文件就在目录里，Agent 为什么没有用上？",
                  "模型这一轮看见的，不是整个工作目录", "谁来准备模型每一轮看到的内容？"]
        checks = {
            "执行期间版本未变": all(digest(root / name) == value for name, value in hashes.items()),
            **shape["checks"],
            "分节与定稿三个正文小节对齐": all(source_notebook.cells[i].source.startswith("## " + title + "\n")
                for i, title in zip((3, 6, 9), stages)),
            "输入目录前后未变": followup["inputs_unchanged"] and
                json.loads((runtime / "inputs-before.json").read_text()) ==
                json.loads((runtime / "inputs-after.json").read_text()) == snapshot(runtime / "work"),
            "三阶段留痕完整": [a["act"] for a in record["acts"]] == stages,
            "连接已关闭": json.loads((runtime / "cleanup.json").read_text())["closed"],
            "输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0),
            "编号符合构造材料约定": bool(re.fullmatch(r"SAMPLE-[0-9a-f]{12}", marker)),
        }
        if mode == "offline":
            checks.update({
                "没有模型请求或适配器日志": not turns and record["request_count"] == 0 and not (runtime / "adapter.log").exists(),
                "离线没有模拟回执或请求装配": not composition["receipts"] and not composition["marker_requests"]
                    and not assembly["requests"] and not followup["old_outputs"] and not followup["retained_outputs"],
                "离线没有模拟模型回答": comparison["before_answer"] is None and comparison["after_answer"] is None
                    and followup["answer"] is None,
            })
        else:
            sys.path.insert(0, str(root / "examples"))
            from notebook_support import command_receipts, tool_output_contains
            requests = [json.loads(p.read_text()) for p in sorted(runtime.glob("request-[0-9]*.json"))]
            before_start, read_start, read_end = [comparison[k] for k in ("before_start", "read_start", "read_end")]
            alert_requests = requests[before_start:read_start]
            read_requests = requests[read_start:read_end]
            followup_requests = requests[read_end:]
            receipts = command_receipts(read_requests)
            observed = requests[before_start:read_end]
            marker_requests = [(i, tool_output_contains(r, marker)) for i, r in enumerate(observed, before_start + 1)]
            first_marker = next((i for i, yes in marker_requests if yes), None)
            previous_ids = {i.get("call_id") for r in observed for i in r.get("input", [])
                            if isinstance(i, dict) and i.get("type") == "function_call"}
            new_calls = [i for r in followup_requests for i in r.get("input", []) if isinstance(i, dict)
                         and i.get("type") == "function_call" and i.get("call_id") not in previous_ids]
            followup_events = [x.get("item", x) for x in turns[-1].get("items", [])] if turns else []
            first_request = observed[0] if observed else {}
            first_items = first_request.get("input", [])
            parts = {
                "基础指令 instructions": bool(first_request.get("instructions")),
                "工具定义 tools": bool(first_request.get("tools")),
                "任务消息 input": any(isinstance(i, dict) and i.get("role") == "user"
                                      and run_info["question"] in json.dumps(i, ensure_ascii=False) for i in first_items),
                "开发者规则在消息中": any(isinstance(i, dict) and i.get("role") == "developer"
                                        and run_info["rule"] in json.dumps(i, ensure_ascii=False) for i in first_items),
            }
            old_outputs = {i.get("call_id"): str(i.get("output", "")) for r in read_requests for i in r.get("input", [])
                           if isinstance(i, dict) and i.get("type") == "function_call_output" and marker in str(i.get("output", ""))}
            retained = [i for i in followup_requests[0].get("input", []) if isinstance(i, dict)
                        and i.get("type") == "function_call_output" and i.get("call_id") in old_outputs
                        and str(i.get("output", "")) == old_outputs[i.get("call_id")]] if followup_requests else []
            checks.update({
                "只依据告警的请求不含文件编号或正文": bool(alert_requests) and all(
                    marker not in json.dumps(r, ensure_ascii=False) and "01:40" not in json.dumps(r, ensure_ascii=False)
                    for r in alert_requests),
                "告警轮没有工具调用": all(not any(isinstance(i, dict) and i.get("type") == "function_call"
                    for i in r.get("input", [])) for r in alert_requests) and
                    not any(x.get("item", x).get("type") in ("commandExecution", "fileChange") for x in turns[0]["items"]),
                "读取轮首次请求仍无文件正文": bool(read_requests) and
                    marker not in json.dumps(read_requests[0], ensure_ascii=False) and
                    "01:40" not in json.dumps(read_requests[0], ensure_ascii=False),
                "成功读取回执含编号与采集时间": any(r["exit_code"] == 0 and "deployment.md" in r["command"]
                    and marker in r["output"] and "02:07" in r["output"] for r in receipts),
                "读取后重新装配并带入编号": len(read_requests) >= 2 and
                    any(tool_output_contains(r, marker) for r in read_requests[1:]),
                "编号首次进入的请求记录一致": first_marker is not None and first_marker > read_start + 1
                    and composition["marker_first_request"] == first_marker and
                    composition["marker_requests"] == [list(x) for x in marker_requests],
                "调用与成功回执按编号配对一致": bool(receipts) and receipts == composition["receipts"],
                "四项请求组成据真实字段核验": all(parts.values()) and composition["parts"] == parts,
                "请求装配展示覆盖告警与读取轮": [r["request"] for r in assembly["requests"]] ==
                    list(range(before_start + 1, read_end + 1)) and
                    [r["marker_in_tool_output"] for r in assembly["requests"]] == [yes for _, yes in marker_requests],
                "同会话完成三轮": len(turns) == 3 and all(t["status"] == "completed" for t in turns)
                    and len({t["thread_id"] for t in turns}) == 1
                    and comparison["thread_id"] == followup["thread_id"] == turns[0]["thread_id"],
                "追问保留原调用编号及原工具结果": bool(retained) and retained == followup["retained_outputs"]
                    and bool(followup_requests) and tool_output_contains(followup_requests[0], marker),
                "追问没有新增读取或其他工具调用": not new_calls and not followup["new_tool_calls"]
                    and not any(i.get("type") in ("commandExecution", "fileChange") for i in followup_events),
                "追问回答使用采集编号与时间": marker in (followup["answer"] or "") and "02:07" in (followup["answer"] or ""),
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

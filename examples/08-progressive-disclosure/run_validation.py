"""08 讲整本核验；offline 只回放材料，live 才检查实际模型请求。"""
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


def source_checks(notebook, directory):
    codes = [c for c in notebook.cells if c.cell_type == "code"]
    lengths = [len(c.source.splitlines()) for c in codes]
    text = "\n".join(c.source for c in notebook.cells)
    question = notebook.cells[0].source.split("## 题面", 1)[1].split("## 参考答案", 1)[0]
    hits = [word for word in ("SKILL.md", "references", "assets", "scripts", "$skill", "description", "渐进披露", "Agent Loop") if word in question]
    checks = {
        "十二格与七个代码格": [c.cell_type for c in notebook.cells] ==
            ["markdown", "markdown", "code", "markdown", "code", "code", "markdown", "code", "code", "markdown", "code", "code"],
        "代码格保留完整调用且不超过三十二行": all(3 <= n <= 32 for n in lengths),
        "准备格七行": len(notebook.cells[2].source.splitlines()) == 7,
        "代码格没有断言": not any("assert" in c.source or any(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(c.source))) for c in codes),
        "源文件无输出": all(not c.outputs and c.execution_count is None for c in codes),
        "开头包含题面和参考答案": bool(question.strip()) and "## 参考答案" in notebook.cells[0].source,
        "题面没有禁用词": not hits,
        "没有元指令词": not any(w in text for w in ("必读", "必跑", "可折叠", "扩展/可选", "公共准备", "九讲统一")),
        "结尾直接收尾": not any(w in "\n".join(c.source for c in notebook.cells[9:]) for w in ("小结", "课后题", "清理")),
        "分节对应定稿": all(title in notebook.cells[index].source for index, title in (
            (3, "一项 Skill 变长以后，为什么不能一次全部装入 Context？"),
            (6, "渐进披露怎样借助 Agent Loop 发生？"), (9, "Harness 怎样把 Skill 接入这条 Loop？"))),
    }
    return {"cell_count": len(notebook.cells), "code_count": len(codes), "code_lines": lengths,
            "question_forbidden_hits": hits, "checks": checks}


def replay_checks(runtime, markers):
    states = json.loads((runtime / "replay-context.json").read_text())
    events = json.loads((runtime / "replay-events.json").read_text())
    first = {name: next((i for i, state in enumerate(states) if marker in state["content"]), None)
             for name, marker in markers.items()}
    script = json.loads((runtime / "script-replay.json").read_text())
    output = json.loads(script["output"])
    return {
        "回放索引仅含名称描述": markers["META"] in states[0]["content"] and all(markers[n] not in states[0]["content"] for n in ("BODY", "REF", "ASSET", "SOURCE", "RESULT")),
        "回放选中后方法进入且附件未装": first["BODY"] == 1 and all(markers[n] not in states[1]["content"] for n in ("REF", "ASSET", "SOURCE", "RESULT")),
        "回放按调查进度读取参考和模板": None not in (first["REF"], first["ASSET"], first["RESULT"]) and
            first["BODY"] < first["REF"] < first["ASSET"] < first["RESULT"] and
            states[first["REF"]]["stage"].endswith("references/log-fields.md") and states[first["ASSET"]]["stage"].endswith("assets/incident-report.md"),
        "本地真实脚本返回缺引用项": script["exit_code"] == 0 and output["标记"] == markers["RESULT"] and output["条目数"] == 2 and output["缺引用条目"] == [2],
        "回放只有脚本结果而无源码": first["SOURCE"] is None and events[-1]["action"] == "本地执行检查脚本",
    }


def live_checks(runtime, requests, markers):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from notebook_support import command_receipts, tool_output_contains
    ranges = json.loads((runtime / "request-ranges.json").read_text())
    catalog = requests[slice(*ranges["catalog"])]
    method = requests[slice(*ranges["method"])]
    investigation = requests[slice(*ranges["investigation"])]
    catalog_text = json.dumps(catalog, ensure_ascii=False)
    method_text = json.dumps(method, ensure_ascii=False)
    receipts = command_receipts(investigation)
    first = {name: next((i for i, r in enumerate(investigation) if tool_output_contains(r, marker)), None)
             for name, marker in markers.items()}
    def returned(name, filename):
        return any(r["exit_code"] == 0 and filename in r["command"] and markers[name] in r["output"] for r in receipts)
    script_results = []
    for receipt in receipts:
        if receipt["exit_code"] == 0 and "check_report.py" in receipt["command"]:
            for line in receipt["output"].splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("标记") == markers["RESULT"]:
                    script_results.append(value)
    return {
        "live初始只见名称描述": bool(catalog) and markers["META"] in catalog_text and all(markers[n] not in catalog_text for n in ("BODY", "REF", "ASSET", "SOURCE", "RESULT")),
        "live选中后完整方法进入": bool(method) and markers["BODY"] in method_text and all(markers[n] not in method_text for n in ("REF", "ASSET", "SOURCE", "RESULT")),
        "live调查初始不携带附件正文": bool(investigation) and all(markers[n] not in json.dumps(investigation[0], ensure_ascii=False) for n in ("REF", "ASSET", "SOURCE", "RESULT")),
        "live参考读取成功且回流后续请求": returned("REF", "log-fields.md") and first["REF"] is not None and first["REF"] > 0,
        "live模板读取成功且回流后续请求": returned("ASSET", "incident-report.md") and first["ASSET"] is not None and first["REF"] is not None and first["REF"] < first["ASSET"],
        "live真实脚本结果回流后续请求": bool(script_results) and all(v.get("条目数", 0) >= 2 and isinstance(v.get("缺引用条目"), list) for v in script_results) and
            first["RESULT"] is not None and first["ASSET"] is not None and first["ASSET"] < first["RESULT"],
        "live脚本源码未常驻": all(markers["SOURCE"] not in json.dumps(r, ensure_ascii=False) for r in requests),
        "live调用与回执配对记录一致": receipts == json.loads((runtime / "receipts.json").read_text()),
    }


def write_report(directory, record):
    sys.path.insert(0, str(directory.parent))
    from validation_report import write_report as write_runtime_report
    write_runtime_report(directory, record)


def run(mode):
    directory = Path(__file__).resolve().parent
    root = directory.parents[1]
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("请选择 Python 3.12 执行验证。")
    if mode == "live" and not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("实际模型核验需要 GLM_API_KEY 环境变量。")
    run_id = "nb08-v1-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True)
    source = directory / "workshop.ipynb"
    files = [source, Path(__file__).resolve(), root / "examples/notebook08_setup.py", root / "examples/notebook08_story.py",
             root / "examples/notebook_support.py", root / "examples/validation_report.py", directory / "README.md",
             *sorted(p for p in (directory / "materials").rglob("*") if p.is_file())]
    hashes = {str(p.relative_to(root)): digest(p) for p in files}
    record = {"mode": mode, "run_id": run_id, "status": "running", "hashes": hashes, "cells": []}
    secret_values = [v for k, v in os.environ.items() if len(v) > 8 and any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
    def clean(text):
        for value in secret_values:
            text = text.replace(value, "<凭证已隐藏>")
        return text.replace(str(root), "<项目根目录>").replace(str(Path.home()), "<用户目录>")
    def save():
        text = clean(json.dumps(record, ensure_ascii=False, indent=2)) + "\n"
        (out / "result.json").write_text(text)
        for name in ("result.json", "status.json"):
            (directory / "validation" / name).write_text(text)
    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    shape = source_checks(notebook, directory)
    record["source_checks"] = shape
    save()
    if not all(shape["checks"].values()):
        raise RuntimeError("源 Notebook 检查未通过。")
    client = NotebookClient(notebook, timeout=1200, kernel_name="python3", allow_errors=False,
                            resources={"metadata": {"path": str(directory)}})
    client.create_kernel_manager()
    client.km.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(root / "examples")
    for key in list(env):
        if any(w in key.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and key != "GLM_API_KEY":
            env.pop(key)
    if mode == "offline":
        env.pop("GLM_API_KEY", None)
        env["LAB_VALIDATION_RUN_ID"] = run_id
    else:
        env.pop("LAB_VALIDATION_RUN_ID", None)
        env["NB08_LIVE_RUN_ID"] = run_id
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
    runtime, kernel_process, start = None, None, time.monotonic()
    try:
        with client.setup_kernel(env=env):
            kernel_process = client.km.provisioner.process
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
                # 模型连接一旦被误启动，立即失败，避免把零请求记录当成充分保护。
                extra('''import notebook_support
def _forbid_model(*args, **kwargs):
    raise RuntimeError("离线模式禁止启动模型连接。")
notebook_support.Lab = _forbid_model
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
                        record["cells"].append({"index": index, "seconds": round(time.monotonic() - cell_start, 3),
                            "status": "failed" if any(o.output_type == "error" for o in cell.get("outputs", [])) else "finished"})
                        save()
                    if index == 2:
                        runtime = Path(extra("print(story.runtime)").strip())
                        record["runtime"] = str(runtime.relative_to(root))
                    print(f"结束 cell{index}", flush=True)
            finally:
                extra('if "story" in globals(): story.close()')
                record["key_inputs"] = int(extra("print(_key_inputs)").strip())
        if runtime is None:
            raise RuntimeError("没有创建本次运行目录。")
        requests = [json.loads(p.read_text()) for p in sorted(runtime.glob("request-[0-9]*.json"))]
        turns = [json.loads(p.read_text()) for p in sorted(runtime.glob("turn-[0-9]*.json"))]
        run_info = json.loads((runtime / "run.json").read_text())
        cleanup = json.loads((runtime / "cleanup.json").read_text())
        decisions = json.loads((runtime / "descriptions.json").read_text())["decisions"]
        budget = json.loads((runtime / "budget.json").read_text())
        before = json.loads((runtime / "inputs-before.json").read_text())
        after = json.loads((runtime / "inputs-after.json").read_text())
        sys.path.insert(0, str(root / "examples"))
        from notebook08_story import snapshot
        actual = {"inputs": snapshot(runtime / "work/inputs"), "skill": snapshot(runtime / "work" / run_info["skill"])}
        checks = {**shape["checks"],
            "执行期间版本未变": all(digest(root / name) == value for name, value in hashes.items()),
            "输入与方法均未改变": before == after == actual,
            "三个阶段均完成": json.loads((runtime / "stages.json").read_text()) == ["阶段一", "阶段二", "阶段三"],
            "字符对照显示提前装入附件": budget["packed_chars"] > budget["core_chars"] and budget["extra_chars"] == budget["packed_chars"] - budget["core_chars"],
            "描述对照显示误选与漏选": decisions == {"太宽": [True, True], "合适": [False, True], "太窄": [False, False]},
            "报告存在且非空": bool((runtime / "work/report.md").read_text().strip()),
            "连接已关闭": cleanup["closed"] and not cleanup["errors"] and not any(cleanup["ports_open"].values()),
            "本次内核进程已退出": kernel_process is not None and kernel_process.poll() is not None,
            "密钥输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0),
        }
        if mode == "offline":
            checks.update(replay_checks(runtime, run_info["markers"]))
            checks["offline无模型请求或代理进程"] = not requests and not turns and not (runtime / "adapter.log").exists() and not cleanup["model_started"]
            checks["offline没有伪造模型回执"] = json.loads((runtime / "receipts.json").read_text()) == []
        else:
            checks.update(live_checks(runtime, requests, run_info["markers"]))
            checks["live三次任务轮正常完成"] = len(turns) == 3 and all(t["status"] == "completed" for t in turns)
            checks["live进程和端口已释放"] = cleanup["proxy_returncode"] is not None and cleanup["model_returncode"] is not None and not any(cleanup["ports_open"].values())
        record.update(checks=checks, request_count=len(requests), turn_count=len(turns),
                      kernel_returncode=kernel_process.poll() if kernel_process else None,
                      status="passed" if all(checks.values()) else "failed")
    except BaseException as exc:
        record.update(status="failed", error_type=type(exc).__name__, error=clean(str(exc)))
    finally:
        record["seconds"] = round(time.monotonic() - start, 3)
        notebook.metadata["validation"] = {k: record.get(k) for k in ("mode", "run_id", "status")}
        (out / "executed.ipynb").write_text(clean(nbformat.writes(notebook)))
        html, _ = HTMLExporter().from_notebook_node(nbformat.read(out / "executed.ipynb", as_version=4))
        (out / "reading.html").write_text(html, encoding="utf-8")
        record.setdefault("checks", {})["阅读页全文没有阶段禁用字"] = chr(0x5e55) not in html
        write_report(directory, record)
        record["checks"]["报告全文没有阶段禁用字"] = chr(0x5e55) not in (directory / "validation/REPORT.md").read_text()
        reports = (directory / "validation/REPORT.md", out / "REPORT.md")
        record["checks"]["两份报告链接均有效"] = all((report.parent / link).exists()
            for report in reports for link in re.findall(r"\]\(([^)]+)\)", report.read_text()))
        if not all(record["checks"].values()):
            record["status"] = "failed"
        write_report(directory, record)
        save()
        (directory / "validation" / (mode + "-latest.json")).write_text(json.dumps({"run_id": run_id, "status": record["status"]}, ensure_ascii=False))
        print(json.dumps({k: record.get(k) for k in ("status", "mode", "seconds", "key_inputs", "request_count", "turn_count")}, ensure_ascii=False), flush=True)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    args = parser.parse_args()
    sys.exit(0 if run(args.mode)["status"] == "passed" else 1)

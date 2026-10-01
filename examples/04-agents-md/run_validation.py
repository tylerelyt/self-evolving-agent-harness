"""04 讲整本执行；分别核验材料回放与真实模型路径。"""
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
    hits = [word for word in ("AGENTS.md", "AGENTS.override.md", "Thread", "agents_md", "agents_md_paths", ".git", "override", "cwd") if word in question]
    tail = "\n".join(c.source for c in notebook.cells[9:])
    checks = {
        "十二格结构与七个代码格": len(notebook.cells) == 12 and len(code) == 7 and
            [c.cell_type for c in notebook.cells] == ["markdown", "markdown", "code", "markdown", "code", "code", "markdown", "code", "code", "markdown", "code", "code"],
        "代码格保留完整调用且不超过三十二行": all(3 <= n <= 32 for n in lengths),
        "代码格没有断言语句": assertions == 0 and not any("assert" in c.source for c in code),
        "源notebook没有执行输出": all(not c.outputs and c.execution_count is None for c in code),
        "题面禁用词命中数为零": not hits,
        "结尾没有额外分节": not any(word in tail for word in ("小结", "课后题", "继续观察", "清理")),
        "全文没有阶段禁用字": "幕" not in text,
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
    run_id = "nb04-v1-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True)
    files = [source, Path(__file__).resolve(), root / "examples/notebook04_setup.py", root / "examples/notebook04_story.py",
             root / "examples/notebook_support.py", directory / "README.md", *sorted(p for p in (directory / "materials").rglob("*") if p.is_file())]
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
        tasks = json.loads((runtime / "tasks.json").read_text())
        assembly = json.loads((runtime / "request-assembly.json").read_text())
        selection = json.loads((runtime / "selection.json").read_text())
        scope = json.loads((runtime / "scope.json").read_text())
        cleanup = json.loads((runtime / "cleanup.json").read_text())
        source_notebook = nbformat.read(source, as_version=4)
        shape = inspect_source(source_notebook)
        record["source_checks"] = shape
        # 标题从实际 Markdown 读取，不给阅读页与报告另造阶段名称。
        stages = [source_notebook.cells[i].source.splitlines()[0].removeprefix("## ") for i in (3, 6, 9)]
        sys.path.insert(0, str(root / "examples"))
        from notebook04_story import request_input, STAGES
        expected_root = (directory / "materials/AGENTS.md").read_text().strip()
        expected_local = (directory / "materials/payment-service/AGENTS.md").read_text().strip()
        expected_override = (directory / "materials/payment-service/AGENTS.override.md").read_text().replace(
            "{{OVERRIDE_TAG}}", run_info["override_tag"]).strip()
        work = runtime / "work"
        before = json.loads((runtime / "inputs-before.json").read_text())
        after = snapshot(work)
        expected_after = {**before, "AGENTS.md": digest(directory / "materials/AGENTS.md"),
                          "payment-service/AGENTS.md": digest(directory / "materials/payment-service/AGENTS.md")}
        checks = {
            "执行期间版本未变": all(digest(root / name) == value for name, value in hashes.items()),
            **shape["checks"],
            "分节与练习机制主线一致": stages == STAGES
                and all("这里起作用的是框架的" in source_notebook.cells[i].source for i in (3, 6, 9)),
            "三阶段留痕完整": [a["act"] for a in record["acts"]] == stages,
            "实验项目有根目录标记": (work / ".git").is_dir(),
            "准备时未提前写入项目规则": not any(Path(name).name.startswith("AGENTS") for name in before),
            "仅写入预期两级规则且事故未变": after == expected_after == json.loads((runtime / "inputs-after.json").read_text()),
            "三条稳定要求与定稿一致": selection["stable_requirements"] == [line.removeprefix("- ")
                for line in expected_root.splitlines() if line.startswith("- ")]
                and all(word not in expected_root for word in ("server-17", "200", "60"))
                and any("server-17" in line and "200" in line and "60" in line for line in selection["task_specific"]),
            "临时规则编号为构造值": bool(re.fullmatch(r"OVERRIDE-[0-9a-f]{16}", run_info["override_tag"]))
                and "构造值" in expected_override,
            "临时文件已撤下": not (work / "payment-service/AGENTS.override.md").exists()
                and cleanup["override_removed"] and scope["override_checks"]["临时文件已撤下"],
            "连接已关闭": cleanup["closed"] and not cleanup["errors"],
            "输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0),
        }
        if mode == "offline":
            checks.update({
                "没有模型请求或适配器日志": not turns and record["request_count"] == 0 and not (runtime / "adapter.log").exists(),
                "离线没有模拟回答或请求装配": not assembly and all(t["answer"] is None and t["thread_id"] is None
                    and t["start"] == t["end"] == 0 for t in tasks.values()),
                "离线未把文件写入冒充框架加载": scope["root_local_positions"] is None
                    and all(scope["override_checks"][key] is None for key in ("根规则仍在", "优先文件已加入", "同层原规则未合并")),
            })
        else:
            requests = [json.loads(p.read_text()) for p in sorted(runtime.glob("request-[0-9]*.json"))]
            texts = {name: [request_input(r) for r in requests[t["start"]:t["end"]]] for name, t in tasks.items()}
            first = {name: values[0] if values else "" for name, values in texts.items()}
            expected_assembly = []
            for name in ("old", "corrected", "unruled", "root", "local", "override"):
                task = tasks[name]
                for index in range(task["start"], task["end"]):
                    text = request_input(requests[index])
                    expected_assembly.append({"task": name, "request": index + 1,
                        "positions": {label: text.find(rule) for label, rule in (
                            ("correction", run_info["correction"]), ("root", expected_root),
                            ("local", expected_local), ("override", expected_override))},
                        "input": text, "first_in_task": index == task["start"]})
            task_turns = {name: turns[i] for i, name in enumerate(("old", "corrected", "unruled", "root", "local", "override"))}
            ids = [tasks[name]["thread_id"] for name in ("old", "unruled", "root", "local", "override")]
            checks.update({
                "六次任务轮正常完成且五个独立会话": len(turns) == 6 and all(t["status"] == "completed" for t in turns)
                    and tasks["old"]["thread_id"] == tasks["corrected"]["thread_id"] and len(set(ids)) == 5
                    and all(tasks[name]["thread_id"] == t["thread_id"] and tasks[name]["answer"] == t["final_response"]
                            for name, t in task_turns.items()),
                "旧初答按明示的旧稿写法生成": "先发生即原因" in first["old"] and "server-17" in first["old"]
                    and run_info["correction"] not in first["old"],
                "旧纠正实际进入旧会话": run_info["correction"] in first["corrected"] and "server-17" in first["corrected"],
                "旧纠正未自动进入新会话": bool(texts["unruled"]) and all(run_info["correction"] not in text
                    and "server-17" not in text and expected_root not in text for text in texts["unruled"]),
                "新任务的事故材料完全相同": all(run_info["new_prompt"] in first[name] for name in ("unruled", "root", "local", "override")),
                "从根启动只加载根规则": expected_root in first["root"] and expected_local not in first["root"]
                    and expected_override not in first["root"] and tasks["root"]["cwd"] == ".",
                "服务目录加载两级且根规则在前": 0 <= first["local"].find(expected_root) < first["local"].find(expected_local)
                    and expected_override not in first["local"] and tasks["local"]["cwd"] == "payment-service",
                "优先文件运行根规则仍在且优先文件已装": expected_root in first["override"] and expected_override in first["override"]
                    and first["override"].find(expected_root) < first["override"].find(expected_override),
                "优先文件替换同层原规则而非合并": expected_local not in first["override"]
                    and "分开核对支付请求异常与下游依赖异常" not in first["override"],
                "请求装配留痕与原始请求一致": assembly == expected_assembly and
                    [r["request"] for r in assembly] == list(range(1, len(requests) + 1)),
                "两级顺序展示据首次实际请求": scope["root_local_positions"] ==
                    [first["local"].find(expected_root), first["local"].find(expected_local)]
                    and all(scope["override_checks"].values()),
                "全部分析未调用工具或重读旧任务": not any(x.get("item", x).get("type") in ("commandExecution", "fileChange")
                    for t in turns for x in t["items"]) and not any(i.get("type") == "function_call"
                    for r in requests for i in r.get("input", []) if isinstance(i, dict)),
            })
            process = json.loads((runtime / "process-cleanup.json").read_text())
            record["process_cleanup"] = process
            checks["进程和端口已释放"] = process["proxy_returncode"] is not None and process["model_returncode"] is not None \
                and not any(process["ports_open"].values()) and not process["errors"]
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
        record.setdefault("checks", {})["阅读页全文没有阶段禁用字"] = "幕" not in html
        if not record["checks"]["阅读页全文没有阶段禁用字"]:
            record["status"] = "failed"
        save()
        print(json.dumps({k: record.get(k) for k in ("status", "mode", "seconds", "key_inputs", "turn_count", "request_count")}, ensure_ascii=False), flush=True)
        (directory / "validation" / (mode + "-latest.json")).write_text(json.dumps({"run_id": run_id, "status": record["status"]}, ensure_ascii=False))
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    args = parser.parse_args()
    sys.exit(0 if run(args.mode)["status"] == "passed" else 1)

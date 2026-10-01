"""05 讲整本执行；分别核验材料回放与真实模型路径。"""
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
    hits = [word for word in ("compact", "contextCompaction", "StoredThreadHistory", "StoredModelContext", "replacement_history", "handoff", "Compaction", "thread/compact/start") if word in question]
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
    run_id = "nb05-v1-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True)
    files = [source, Path(__file__).resolve(), root / "examples/notebook05_setup.py", root / "examples/notebook05_story.py",
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
        before = json.loads((runtime / "before.json").read_text())
        after = json.loads((runtime / "after.json").read_text())
        recall = json.loads((runtime / "recall.json").read_text())
        shape = inspect_source(nbformat.read(source, as_version=4))
        record["source_checks"] = shape
        stages = ["历史被压缩以后，调查还能接着做吗？", "上下文更新后，什么不该随之改变？", "Codex 怎样做 Compaction？"]
        checks = {
            "执行期间版本未变": all(digest(root / name) == value for name, value in hashes.items()),
            **shape["checks"],
            "分节与定稿三个正文小节对齐": all(notebook.cells[i].source.startswith("## " + title + "\n")
                for i, title in zip((3, 6, 9), stages)),
            "三阶段留痕完整": [a["act"] for a in record["acts"]] == stages,
            "输入文件未变": recall["inputs_unchanged"] and json.loads((runtime / "inputs-before.json").read_text()) ==
                json.loads((runtime / "inputs-after.json").read_text()) == snapshot(runtime / "work"),
            "连接已关闭": json.loads((runtime / "cleanup.json").read_text())["closed"],
            "输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0),
        }
        sys.path.insert(0, str(root / "examples"))
        from notebook05_story import available_samples, input_text, sample_lines, sample_values
        disk = (runtime / "work/capacity.log").read_text(encoding="utf-8")
        expected = sample_values(disk)
        expected["difference"] = expected["sample_0127"] - expected["sample_0073"]
        checks["日志保留180采样与末尾调查材料并标注构造值"] = (
            len(re.findall(r"^sample=", disk, re.M)) == 180 and "构造值" in disk and
            all(x in disk for x in ("CASE-527", "01:30", "01:40", "02:07", "pool_wait_ms=3600")))
        checks["精确对照值来自磁盘原文"] = recall["expected"] == expected and recall["samples"] == sample_lines(disk)
        if mode == "offline":
            checks.update({
                "没有模型请求或适配器日志": not turns and record["request_count"] == 0 and not (runtime / "adapter.log").exists(),
                "离线没有模拟模型回答或复述": before["first_answer"] is None and before["corrected_answer"] is None and
                    after["answer"] is None and not after["state"] and recall["answer"] is None and recall["exact"] is None,
                "离线没有模拟压缩或历史及工具回执": not (runtime / "compaction.json").exists() and
                    not before["turn_ids"] and after["history"] is None and not before["receipts"] and not recall["new_reads"],
            })
        else:
            from notebook_support import command_receipts
            requests = [json.loads(p.read_text()) for p in sorted(runtime.glob("request-[0-9]*.json"))]
            compaction = json.loads((runtime / "compaction.json").read_text())
            compact_turn = compaction["turn"]
            state, exact = after["state"], recall["exact"]
            history = after["history"]
            history_ids = {t["id"] for t in history["turns"]}
            history_text = json.dumps(history, ensure_ascii=False)
            before_request = requests[before["request_index"] - 1]
            after_request = requests[after["request_index"]]
            recall_requests = requests[recall["request_start"]:]
            previous_ids = {r["call_id"] for r in command_receipts(requests[:recall["request_start"]])}
            new_reads = [r for r in command_receipts(recall_requests) if r["call_id"] not in previous_ids]
            available = available_samples(input_text(recall_requests[0]), sample_lines(disk)) if recall_requests else []
            read_outputs = "\n".join(r["output"] for r in new_reads if r["exit_code"] == 0 and "capacity.log" in r["command"])
            read_available = available_samples(read_outputs, sample_lines(disk))
            initial_receipts = command_receipts(requests[:before["request_index"]])
            record["compaction_turn_count"] = 1
            record["before_chars"], record["after_chars"] = after["before_chars"], after["after_chars"]
            record["recall_path"] = "当前输入已有数值" if all(available) else "新增工具回读"
            checks.update({
                "同一会话四个调查轮均完成": len(turns) == 4 and all(t["status"] == "completed" for t in turns) and
                    len({t["thread_id"] for t in turns}) == 1 and compaction["thread_id"] == turns[0]["thread_id"] == history["id"],
                "真实读取原始日志": any(r["exit_code"] == 0 and
                    ("capacity.log" in r["command"] or all(line in r["output"] for line in sample_lines(disk))) and
                    "CASE-527" in r["output"] for r in initial_receipts),
                "本次新增压缩项已完成": compact_turn["id"] not in before["turn_ids"] and compact_turn["status"] == "completed" and
                    any(i.get("type") == "contextCompaction" for i in compact_turn["items"]) and compact_turn["id"] in history_ids,
                "字符数依据真实前后请求": after["before_chars"] == len(input_text(before_request)) and
                    after["after_chars"] == len(input_text(after_request)),
                "后续输入比压缩前短": after["after_chars"] < after["before_chars"],
                "原调查轮次仍可读且采样原文仍在完整历史": set(before["turn_ids"]) <= history_ids and
                    all(line in history_text for line in sample_lines(disk)),
                "任务编号保留": state.get("task") == "CASE-527",
                "发布仍为待验证而非已排除": state.get("release_status") == "待验证",
                "发布前报错的理由保留": isinstance(state.get("reason"), str) and "发布" in state["reason"] and
                    any(x in state["reason"] for x in ("前", "早于", "之前", "先于")) and
                    any(x in state["reason"] for x in ("报错", "错误", "超时", "timeout", "Timeout")),
                "连接池方向保留": isinstance(state.get("next_step"), str) and "连接池" in state["next_step"],
                "等待与错误率同时升高的理由保留": isinstance(state.get("next_reason"), str) and
                    any(x in state["next_reason"] for x in ("等待", "wait", "pool_wait")) and
                    any(x in state["next_reason"] for x in ("错误率", "报错率", "error_rate", "error")) and
                    any(x in state["next_reason"] for x in ("同时", "同一", "同步", "一起")),
                "不许重启限制保留": isinstance(state.get("constraint"), str) and "重启" in state["constraint"] and
                    any(x in state["constraint"] for x in ("不", "禁止", "仅", "只")),
                "精确数值及差值与磁盘原文一致": isinstance(exact, dict) and all(type(exact.get(k)) is int and exact[k] == v for k, v in expected.items()),
                "精确回答有输入依据或成功回读": bool(available) and all(a or b for a, b in zip(available, read_available)),
                "回读记录与真实请求相符": recall["new_reads"] == new_reads and recall["available_before"] == available,
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
        if runtime is not None:
            record["turn_count"] = len(list(runtime.glob("turn-[0-9]*.json")))
            record["request_count"] = len(list(runtime.glob("request-[0-9]*.json")))
            closed = runtime / "cleanup.json"
            process = runtime / "process-cleanup.json"
            if closed.exists():
                record.setdefault("checks", {})["连接已关闭"] = json.loads(closed.read_text())["closed"]
            if process.exists():
                record["process_cleanup"] = json.loads(process.read_text())
        notebook.metadata["validation"] = {k: record.get(k) for k in ("mode", "run_id", "status")}
        (out / "executed.ipynb").write_text(clean(nbformat.writes(notebook)))
        exporter = HTMLExporter()
        html, _ = exporter.from_notebook_node(nbformat.read(out / "executed.ipynb", as_version=4))
        (out / "reading.html").write_text(html, encoding="utf-8")
        from html.parser import HTMLParser
        class Tables(HTMLParser):
            def __init__(self):
                super().__init__()
                self.count, self.left, self.headers = 0, True, []
                self.in_header = False
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "div" and "act-result" in attrs.get("class", "").split():
                    self.count += 1
                if tag in ("td", "th"):
                    self.left &= "text-align:left" in attrs.get("style", "")
                if tag == "th": self.in_header = True
            def handle_endtag(self, tag):
                if tag == "th": self.in_header = False
            def handle_data(self, data):
                if self.in_header: self.headers.append(data)
        tables = Tables()
        tables.feed(html)
        html_checks = {"阅读页没有禁用汉字": "\u5e55" not in html,
                       "阅读页有三张左对齐过程表": tables.count == 3 and tables.left,
                       "过程表头符合任务书": tables.headers == ["压缩前的依据", "实际内容", "比较项", "压缩前", "压缩后", "保存位置", "采样原文是否可找到"]}
        record["checks"] = {**record.get("checks", {}), **html_checks}
        if record["status"] == "passed" and not all(html_checks.values()):
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

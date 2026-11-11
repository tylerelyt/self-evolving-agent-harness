"""09 讲整本核验；offline 回放材料，live 才检查原生 Sandbox 和审批请求。"""
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

STAGES = ["最省事的方案，为什么不能让它自己改？", "计划里已经说“不要改”，为什么还不够？", "怎样把计划真正变成权限？"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_checks(notebook, directory):
    codes = [c for c in notebook.cells if c.cell_type == "code"]
    lengths = [len(c.source.splitlines()) for c in codes]
    checks = {
        "执行单元格布局与验证器一致": len(codes) == 13 and len(notebook.cells) == 23,
        "代码格可解析": all(ast.parse(c.source) is not None for c in codes),
        "源Notebook无执行输出": all(not c.outputs and c.execution_count is None for c in codes),
        "业务修改前先完成权限自检": next(i for i, c in enumerate(notebook.cells) if "story.record_selfcheck(" in c.source)
            < next(i for i, c in enumerate(notebook.cells) if c.get("id") == "lab09-07")
            and "require_checked_permission" in next(c.source for c in notebook.cells if c.get("id") == "lab09-07"),
    }
    return {"cell_count": len(notebook.cells), "code_count": len(codes), "code_lines": lengths, "checks": checks}




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
    run_id = "nb09-v1-" + mode + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    out = directory / "validation/runs" / run_id
    out.mkdir(parents=True)
    source = directory / "workshop.ipynb"
    files = [source, Path(__file__).resolve(), root / "examples/notebook09_setup.py", root / "examples/notebook09_story.py",
             root / "examples/notebook_support.py", root / "examples/validation_report.py", directory / "README.md",
             *sorted(p for p in (directory / "materials").rglob("*") if p.is_file() and "__pycache__" not in p.parts)]
    hashes = {str(p.relative_to(root)): digest(p) for p in files}
    record = {"mode": mode, "run_id": run_id, "status": "running", "hashes": hashes, "cells": []}
    secret_values = [v for k, v in os.environ.items() if len(v) > 8 and any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]

    def clean(text):
        for value in secret_values:
            text = text.replace(value, "<凭证已隐藏>")
        return text.replace(str(root), "<项目根目录>").replace(str(Path.home()), "<用户目录>")

    def save():
        content = clean(json.dumps(record, ensure_ascii=False, indent=2)) + "\n"
        for folder in (out, directory / "validation"):
            for name in ("result.json", "status.json"):
                (folder / name).write_text(content)

    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    shape = source_checks(notebook, directory)
    record["source_checks"] = shape
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(root / "examples")
    for key in list(env):
        if any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and (mode == "offline" or key != "GLM_API_KEY"):
            env.pop(key, None)
    if mode == "offline":
        env["LAB_VALIDATION_RUN_ID"] = run_id
    else:
        env.pop("LAB_VALIDATION_RUN_ID", None)
    for name, folder in (("IPYTHONDIR", "ipython"), ("JUPYTER_RUNTIME_DIR", "jupyter")):
        (out / folder).mkdir()
        env[name] = str(out / folder)
    client = NotebookClient(notebook, timeout=1200, kernel_name="python3", allow_errors=False,
                            resources={"metadata": {"path": str(directory)}})
    client.create_kernel_manager()
    client.km.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]

    def extra(code):
        cell = nbformat.v4.new_code_cell(code)
        notebook.cells.append(cell)
        try:
            client.execute_cell(cell, len(notebook.cells) - 1, store_history=False)
        finally:
            notebook.cells.pop()
        return "".join(o.get("text", "") for o in cell.get("outputs", []))

    start, runtime, kernel_process = time.monotonic(), None, None
    save()
    try:
        if not all(shape["checks"].values()):
            raise RuntimeError("源 Notebook 结构检查失败。")
        with client.setup_kernel(env=env):
            kernel_process = client.km.provisioner.process
            extra('''import builtins, getpass, os
_key_inputs = 0
_review_inputs = 0
_review_pending = False
_test_key = os.environ.pop("GLM_API_KEY", "")
def _key_input(prompt):
    global _key_inputs
    _key_inputs += 1
    return _test_key
getpass.getpass = _key_input
get_ipython().kernel.getpass = _key_input
def _review_input(prompt):
    global _review_inputs, _review_pending
    if not _review_pending or prompt != _review_prompt:
        raise RuntimeError("未提供与本轮展示内容对应的确认输入。")
    _review_pending = False
    _review_inputs += 1
    return _review_answer
builtins.input = _review_input
get_ipython().kernel.raw_input = _review_input
''')
            if mode == "offline":
                extra('''import notebook_support, sys

def _forbid_model(*args, **kwargs):
    raise RuntimeError("离线模式禁止启动模型连接。")
notebook_support.Lab = _forbid_model

def _offline_audit(event, args):
    if event == "socket.connect" and isinstance(args[1], tuple) and args[1][0] not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError("离线模式禁止外部网络。")
    if event == "subprocess.Popen":
        argv = args[1]
        if not (isinstance(argv, (list, tuple)) and len(argv) == 3 and argv[0] == sys.executable and argv[1] == "-B"
                and argv[2].endswith(("/test_adapter.py", "/write_report.py"))):
            raise RuntimeError("离线模式只允许本例教学子进程。")
sys.addaudithook(_offline_audit)
''')
            try:
                for index, cell in enumerate(notebook.cells):
                    if cell.cell_type != "code":
                        continue
                    print(f"开始 cell{index}", flush=True)
                    cell_start = time.monotonic()
                    try:
                        confirmation = {
                            "lab09-confirm-plan": ("确认计划", "plan.final_response if lab else story.plan"),
                            "lab09-confirm-config": ("应用配置", 'json.dumps({"plan": story.confirmed_plan, "config": proposal}, ensure_ascii=False, indent=2)'),
                        }.get(cell.get("id"))
                        if confirmation:
                            token, content = confirmation
                            print(extra(f"print({content})"), flush=True)
                            prompt = f"输入“{token}”继续，其他输入停止："
                            # offline 仅回放输入；live 必须在终端由学员实际确认。
                            answer = token if mode == "offline" else input(prompt)
                            extra(f"_review_prompt = {prompt!r}\n_review_answer = {answer!r}\n_review_pending = True")
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
                record["review_inputs"] = int(extra("print(_review_inputs)").strip())
        if runtime is None:
            raise RuntimeError("没有创建运行目录。")
        requests = [json.loads(p.read_text()) for p in sorted(runtime.glob("request-[0-9]*.json"))]
        turns = [json.loads(p.read_text()) for p in sorted(runtime.glob("turn-[0-9]*.json"))]
        cleanup = json.loads((runtime / "cleanup.json").read_text())
        observations = json.loads((runtime / "observations.json").read_text())
        boundary = json.loads((runtime / "plan-boundary.json").read_text())
        approval = json.loads((runtime / "approval-result.json").read_text())
        approvals = json.loads((runtime / "approvals.json").read_text())
        reviewed = json.loads((runtime / "reviewed-config.json").read_text())
        confirmations = json.loads((runtime / "confirmations.json").read_text())
        permission = reviewed["approved"]
        work = runtime / "work"
        sys.path.insert(0, str(root / "examples"))
        from notebook09_story import snapshot
        before = json.loads((runtime / "inputs-before.json").read_text())
        after = snapshot(work)
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        expected_changes = {"services/payment/legacy_adapter/adapter.py", "services/payment/legacy_adapter/allowed.txt",
            "services/payment/legacy_adapter/report.txt", "tests/payment/legacy_adapter/test_adapter.py", "tests/payment/legacy_adapter/allowed.txt"}
        checks = {**shape["checks"],
            "执行期间版本未变": all(digest(root / name) == value for name, value in hashes.items()),
            "原材料未变": all(digest(root / name) == value for name, value in hashes.items() if "/materials/" in name),
            "三个阶段完整记录": [x["title"] for x in json.loads((runtime / "stages.json").read_text())] == STAGES,
            "工作目录只有授权修改": changed == expected_changes and after == json.loads((runtime / "inputs-after.json").read_text()),
            "工作目录适配层可写": (work / "services/payment/legacy_adapter/allowed.txt").read_text() == "allowed",
            "额外测试根可写": (work / "tests/payment/legacy_adapter/allowed.txt").read_text() == "tests-allowed",
            "共享默认值哈希未变": digest(work / "lib/database/pool.py") == digest(directory / "materials/lib/database/pool.py"),
            "共享写入被拒": observations["hotfix"]["denied"],
            "只读规划写入被拒": observations["readonly"]["denied"] and not (work / "services/payment/legacy_adapter/readonly-probe.txt").exists(),
            "配置与计划精确对齐": reviewed["proposal"] == permission and permission["cwd"] == str(work / "services/payment/legacy_adapter")
                and permission["sandboxPolicy"]["writableRoots"] == [str(work / "tests/payment/legacy_adapter")],
            "先确认本轮计划再确认对应配置": record["review_inputs"] == 2
                and [c["kind"] for c in confirmations] == ["plan", "config"] and all(c["accepted"] for c in confirmations)
                and confirmations[1]["content"] == {"plan": confirmations[0]["content"], "config": permission}
                and all(c["source"] == ("offline-input-replay" if mode == "offline" else "learner-input") for c in confirmations),
            "网络关闭且临时目录例外关闭": permission["sandboxPolicy"]["networkAccess"] is False
                and permission["sandboxPolicy"]["excludeTmpdirEnvVar"] is True and permission["sandboxPolicy"]["excludeSlashTmp"] is True,
            "主任务不申请扩大权限": permission["approvalPolicy"] == "never" and boundary["expanded_config_rejected"] and boundary["permission_unchanged"],
            "独立参数回归通过": boundary["regression"]["exit_code"] == 0 and "OK" in boundary["regression"]["stderr"],
            "临时文件自检成功与失败配对": observations["selfcheck"]["allowed_exists"] and observations["selfcheck"]["denied"] and observations["selfcheck"]["denied_absent"] and cleanup["selfcheck_removed"],
            "临时文件自检发生在业务修改前": observations["selfcheck"]["business_files_unchanged"],
            "审批先拒绝后批准一次再拒绝": [a["decision"] for a in approvals if a["matched"]] == ["decline", "accept", "decline"]
                and approval["absent_after_denial"] and approval["consumed"] and approval["repeat_decision"] == "decline" and approval["exit_code"] == 0,
            "批准脚本未被改写": digest(work / "services/payment/legacy_adapter/write_report.py") == before["services/payment/legacy_adapter/write_report.py"],
            "构造报告真实生成": (work / "services/payment/legacy_adapter/report.txt").read_text() == "已批准的构造报告",
            "连接已关闭": cleanup["closed"] and not cleanup["errors"] and not any(cleanup["ports_open"].values()),
            "本次内核进程已退出": kernel_process is not None and kernel_process.poll() is not None,
            "密钥输入次数符合模式": record["key_inputs"] == (1 if mode == "live" else 0),
        }
        if mode == "offline":
            replay = json.loads((runtime / "replay-events.json").read_text())
            checks.update({
                "offline无模型请求或代理进程": not requests and not turns and not (runtime / "adapter.log").exists() and not cleanup["model_started"],
                "offline没有伪造模型回答与原生回执": boundary["model_plan"] is None and not approval["thread_ids"]
                    and all(o["source"] == "offline-boundary-replay" and o["receipts"] == [] for o in observations.values())
                    and all(a["source"] == "offline-callback-replay" for a in approvals),
                "回放默认拒绝共享与只读写入": len(replay) == 8 and sum(not e["allowed"] for e in replay) == 3,
            })
        else:
            from notebook_support import command_receipts
            receipts = command_receipts(requests)
            submitted = json.loads((runtime / "submitted-policies.json").read_text())
            checks.update({
                "live执行端收到已核对的封闭权限": len(submitted) == 5 and all(p == permission for p in submitted if p["sandboxPolicy"]["type"] == "workspaceWrite")
                    and sum(p["sandboxPolicy"]["type"] == "workspaceWrite" for p in submitted) == 2,
                "live原生权限拒绝有有效回执": all(o["source"] == "native-sandbox" and o["receipts"] and o["denied"] for o in observations.values()),
                "live规划成功读取共享接口": any(r["exit_code"] == 0 and "wait_seconds=30" in r["output"] for r in receipts),
                "live模型轮正常完成": len(turns) == 7 and all(t["status"] == "completed" for t in turns),
                "live审批使用两个独立任务": len(set(approval["thread_ids"])) == 2 and approval["source"] == "native-client",
                "live进程与端口释放": all(cleanup[name] is not None for name in ("proxy_returncode", "model_returncode", "policy_returncode", "approval_returncode")) and not any(cleanup["ports_open"].values()),
            })
        record.update(checks=checks, request_count=len(requests), turn_count=len(turns), kernel_returncode=kernel_process.poll(),
            status="passed" if all(checks.values()) else "failed")
    except BaseException as exc:
        record.update(status="failed", error_type=type(exc).__name__, error=clean(str(exc)))
    finally:
        record["seconds"] = round(time.monotonic() - start, 3)
        notebook.metadata["validation"] = {k: record.get(k) for k in ("mode", "run_id", "status")}
        (out / "executed.ipynb").write_text(clean(nbformat.writes(notebook)))
        html, _ = HTMLExporter().from_notebook_node(nbformat.read(out / "executed.ipynb", as_version=4))
        html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
        (out / "reading.html").write_text(html)
        record.setdefault("checks", {})["阅读页无外部资源请求"] = not re.search(r"(?:src|href)=[\"\']https?://", html)
        write_report(directory, record)
        record["checks"]["两份报告链接均有效"] = all((report.parent / link).exists() for report in (directory / "validation/REPORT.md", out / "REPORT.md")
            for link in re.findall(r"\]\(([^)]+)\)", report.read_text())) if record.get("runtime") else False
        if not all(record["checks"].values()):
            record["status"] = "failed"
        write_report(directory, record)
        save()
        (directory / "validation" / (mode + "-latest.json")).write_text(json.dumps({"run_id": run_id, "status": record["status"]}, ensure_ascii=False))
        print(json.dumps({k: record.get(k) for k in ("status", "mode", "seconds", "key_inputs", "request_count", "turn_count", "error")}, ensure_ascii=False), flush=True)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    args = parser.parse_args()
    sys.exit(0 if run(args.mode)["status"] == "passed" else 1)

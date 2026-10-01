"""用构造反例检查观察逻辑；不把离线记录当成模型选择结果。"""
import ast
import json
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType
from unittest.mock import patch


def run():
    here = Path(__file__).resolve().parent
    notebook = json.loads((here / "workshop.ipynb").read_text())
    code = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    trees = [ast.parse("".join(c["source"])) for c in code]
    checks = {
        "源Notebook可解析且无输出": all(not c["outputs"] and c["execution_count"] is None for c in code),
        "主路径不构造SkillInput强选": not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "SkillInput" for t in trees for n in ast.walk(t)),
    }
    # 只加载公共文件中的两个纯函数，不导入或启动 SDK/模型连接。
    tree = ast.parse((here.parent / "notebook_support.py").read_text())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("command_receipts", "tool_output_contains")]
    support = ModuleType("notebook_support")
    support.__dict__.update(json=json, re=re)
    exec(compile(ast.Module(body=functions, type_ignores=[]), "receipt-functions", "exec"), support.__dict__)
    from run_validation import live_checks
    markers = {n: "unit-" + n for n in ("META", "BODY", "REF", "ASSET", "SOURCE", "RESULT")}
    unrelated = {"input": [{"role": "system", "content": markers["META"]}]}
    matching = [{"input": [{"role": "system", "content": markers["META"]}]}]
    history = list(matching[0]["input"])
    for i, (part, command) in enumerate((("BODY", "cat SKILL.md"), ("REF", "cat references/log-fields.md"),
                                        ("ASSET", "cat assets/incident-report.md"), ("RESULT", "python scripts/check_report.py report.md"))):
        output = markers[part] if part != "RESULT" else json.dumps({"标记": markers[part], "条目数": 2, "缺引用条目": []}, ensure_ascii=False)
        history += [{"type": "function_call", "name": "exec_command", "call_id": str(i), "arguments": json.dumps({"cmd": command})},
                    {"type": "function_call_output", "call_id": str(i), "output": "Process exited with code 0\n" + output}]
        matching.append({"input": list(history)})
    (here / ".runtime").mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="offline-check-", dir=here / ".runtime") as name, patch.dict(sys.modules, {"notebook_support": support}):
        work = Path(name)
        (work / "request-ranges.json").write_text(json.dumps({"unrelated": [0, 1], "matching": [1, 1 + len(matching)]}))
        (work / "selection-threads.json").write_text(json.dumps({"unrelated": "unit-a", "matching": "unit-b", "explicit_skill_input": False}))
        def inspect(requests):
            (work / "receipts.json").write_text(json.dumps(support.command_receipts(requests[1:])))
            return live_checks(work, requests, markers)
        good = [unrelated, *matching]
        checks["构造的成功配对可被识别"] = all(inspect(good).values())
        fake = json.loads(json.dumps(good))
        for request in fake[1:]:
            for item in request["input"]:
                if item.get("type") == "function_call_output" and item.get("call_id") == "0":
                    item.clear()
                    item.update(role="user", content=markers["BODY"])
        checks["用户文字不能冒充正文读取回执"] = not inspect(fake)["live匹配任务自行读取方法正文"]
        wrong = json.loads(json.dumps(good))
        for request in wrong[1:]:
            for item in request["input"]:
                if item.get("type") == "function_call_output" and item.get("call_id") == "0":
                    item["call_id"] = "unmatched"
        checks["错配调用编号不能冒充正文读取"] = not inspect(wrong)["live匹配任务自行读取方法正文"]
        selected = [matching[1], *matching]
        checks["无关任务读Skill会暴露误触发"] = not inspect(selected)["live无关任务没有读取Skill正文或附件"]
    report = {"mode": "offline-check", "status": "passed" if all(checks.values()) else "failed",
              "checks": checks, "model_requests": 0, "live_validated": False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    sys.exit(0 if run()["status"] == "passed" else 1)

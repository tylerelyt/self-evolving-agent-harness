"""只检查源文件、真实初始化和快照隔离，不生成或冒充模型 Skill。"""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def run():
    here = Path(__file__).resolve().parent
    notebook = json.loads((here / "workshop.ipynb").read_text())
    cells = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    sources = {c["id"]: "".join(c["source"]) for c in cells}
    for source in sources.values():
        ast.parse(source)
    checks = {
        "代码可解析且源Notebook无输出": all(c["execution_count"] is None and not c["outputs"] for c in cells),
        "主路径不复制预置初稿或答案": all(name not in "\n".join(sources.values()) for name in ("first-draft-SKILL.md", "reviewed-SKILL.md")),
        "正文第一版只初始化操作说明": "--resources" not in "".join(notebook["cells"][4]["source"])
            and "第一版只保留操作说明" in "".join(notebook["cells"][5]["source"]),
    }
    (here / ".runtime").mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="offline-check-", dir=here / ".runtime") as name:
        work = Path(name)
        env = {"PATH": os.defpath, "HOME": name, "PYTHONDONTWRITEBYTECODE": "1"}
        initializer = here / "skill_creator/scripts/init_skill.py"
        for skill, resources in (("plain-probe", []), ("resource-probe", ["--resources", "scripts,references,assets"])):
            subprocess.run([sys.executable, "-B", str(initializer), skill, "--path", name, *resources],
                           check=True, capture_output=True, text=True, env=env)
        plain, resource = work / "plain-probe", work / "resource-probe"
        checks["不传resources时没有资源目录"] = all(not (plain / x).exists() for x in ("scripts", "references", "assets"))
        checks["resources实际创建所选空目录"] = all((resource / x).is_dir() and not list((resource / x).iterdir()) for x in ("scripts", "references", "assets"))
        result = subprocess.run([sys.executable, "-B", str(here / "skill_creator/scripts/quick_validate.py"), str(resource)],
                                capture_output=True, text=True, env=env)
        checks["初始化骨架不能冒充完成Skill"] = result.returncode != 0
        tree = ast.parse(sources["880947d2"])
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "skill_snapshot")
        scope = {"hashlib": hashlib}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "notebook-snapshot", "exec"), scope)
        snapshot = scope["skill_snapshot"]
        saved = work / "saved-scaffold"
        shutil.copytree(resource, saved)
        before = snapshot(saved)
        (resource / "assets/change.txt").write_text("离线快照反例，不是模型生成结果。", encoding="utf-8")
        checks["资源变化可见且原稿副本不受影响"] = snapshot(saved) == before and snapshot(resource) != before
    report = {"mode": "offline-check", "status": "passed" if all(checks.values()) else "failed",
              "checks": checks, "model_requests": 0, "live_validated": False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    sys.exit(0 if run()["status"] == "passed" else 1)

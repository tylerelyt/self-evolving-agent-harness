#!/usr/bin/env python3
"""执行本讲 Notebook；离线检查与真实模型观察分别记录，不使用历史回放。"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from uuid import uuid4


def main():
    os.umask(0o077)
    lesson = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("live", "offline"), required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", args.run_id):
        parser.error("运行编号只能使用字母数字、下划线与连字符")
    if args.mode == "live" and not os.environ.get("GLM_API_KEY", "").strip():
        parser.error("缺少 GLM_API_KEY；真实模型运行需要当前进程提供凭据")
    if importlib.util.find_spec("nbclient") is None:
        python = lesson.parents[1]/".venv/bin/python"
        if not python.is_file() or os.environ.get("LAB_NOTEBOOK_REEXEC"):
            raise RuntimeError("请先安装 examples/requirements-notebooks.txt 的 Notebook 依赖")
        os.environ["LAB_NOTEBOOK_REEXEC"] = "1"
        os.execv(str(python), [str(python), "-B", str(Path(__file__).resolve()), *sys.argv[1:]])
    if shutil.disk_usage(lesson).free < 1024**3:
        raise RuntimeError("可用空间不足1GiB，停止生成验证记录")
    out = lesson/"validation/runs"/args.run_id
    out.mkdir(parents=True, exist_ok=False)
    native_run = lesson/"runtime"/("native-notebook-" + uuid4().hex)
    os.environ["LESSON16_RUN_DIR"] = str(native_run)
    os.environ.update(
        PYTHONDONTWRITEBYTECODE="1",
        JUPYTER_RUNTIME_DIR=str(out/"jupyter"),
        JUPYTER_CONFIG_DIR=str(out/"jupyter-config"),
        JUPYTER_CONFIG_PATH=str(out/"jupyter-config"),
        IPYTHONDIR=str(out/"ipython"),
        LESSON16_RUN_MODEL="1" if args.mode == "live" else "0",
        LESSON16_VALIDATION_ID=args.run_id,
    )
    if args.mode == "offline":
        os.environ.pop("GLM_API_KEY", None)
    import nbformat
    from nbclient import NotebookClient
    from jupyter_client import AsyncKernelManager
    from jupyter_client.kernelspec import KernelSpecManager
    from common import scrub
    kernels = out/"kernels/python3"
    kernels.mkdir(parents=True)
    (kernels/"kernel.json").write_text(json.dumps({
        "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "本轮 Python", "language": "python",
    }))

    class RunKernelManager(AsyncKernelManager):
        def __init__(self, **kwargs):
            super().__init__(kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(kernels.parent)]), **kwargs)

    notebook = nbformat.read(lesson/"workshop.ipynb", as_version=4)
    started = time.monotonic()
    status, failure, result = "failed", None, None
    try:
        NotebookClient(notebook, timeout=960, kernel_name="python3",
                       kernel_manager_class=RunKernelManager,
                       resources={"metadata": {"path": str(lesson)}}).execute()
        if args.mode == "live":
            result = json.loads((lesson/"validation"/("notebook-" + args.run_id + ".json")).read_text())
            if (result.get("validation_run_id") != args.run_id or result.get("attempt_id") != native_run.name
                    or Path(result.get("run", "")).resolve() != native_run.resolve()):
                raise RuntimeError("没有对应本次运行编号的原生观察记录")
            if result.get("status") not in ("passed", "no-update", "not-adopted"):
                raise RuntimeError("本次模型观察未完成")
        status = "passed"
    except Exception as error:
        failure = scrub(type(error).__name__ + ": " + str(error))
    for cell in notebook.cells:
        if cell.cell_type == "code":
            for output in cell.outputs:
                if "text" in output:
                    output["text"] = scrub(output["text"])
                if "traceback" in output:
                    output["traceback"] = scrub(output["traceback"])
    nbformat.write(notebook, out/"executed.ipynb")
    summary = {
        "run_id": args.run_id, "status": status, "mode": args.mode,
        "path": "native-live" if args.mode == "live" else "offline-selfcheck",
        "elapsed_seconds": round(time.monotonic()-started, 3), "error": failure,
        "workshop_result": result, "notebook": str(out/"executed.ipynb"),
    }
    if args.mode == "offline":
        summary["actual_model_requests"] = 0
    (out/"result.json").write_text(json.dumps(scrub(summary), ensure_ascii=False, indent=2))
    print(json.dumps(scrub(summary), ensure_ascii=False, indent=2))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

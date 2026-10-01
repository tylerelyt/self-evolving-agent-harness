#!/usr/bin/env python3
"""执行完整 notebook 并保存同版本输出；用项目 .venv 运行。"""
import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('live', 'offline'), required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    import re
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', args.run_id):
        parser.error('运行编号只能使用字母数字、下划线与连字符')
    lesson = Path(__file__).resolve().parent
    out = lesson / 'validation/runs' / args.run_id
    out.mkdir(parents=True, exist_ok=False)
    os.environ.update(PYTHONDONTWRITEBYTECODE='1', JUPYTER_RUNTIME_DIR=str(out / 'jupyter'),
                      IPYTHONDIR=str(out / 'ipython'))
    os.environ.pop('BIGMODEL_API_KEY', None)
    if args.mode == 'offline':
        os.environ.pop('GLM_API_KEY', None)
        os.environ['LAB_VALIDATION_RUN_ID'] = args.run_id
    else:
        os.environ.pop('LAB_VALIDATION_RUN_ID', None)
        sys.path.insert(0, str(lesson))
        from credentials import load_credentials
        load_credentials()
    import nbformat
    from nbclient import NotebookClient
    from nbconvert import HTMLExporter
    started = time.monotonic()
    notebook = nbformat.read(lesson / 'workshop.ipynb', as_version=4)
    latest = lesson / 'validation' / (args.mode + '-latest.json')
    previous_result = latest.read_bytes() if latest.exists() else None
    status = 'failed'; failure = None
    try:
        from jupyter_client.kernelspec import KernelSpecManager
        from jupyter_client import AsyncKernelManager
        kernels = out / 'kernels/python3'
        kernels.mkdir(parents=True)
        (kernels / 'kernel.json').write_text(json.dumps({'argv': [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}'], 'display_name': '本轮 Python', 'language': 'python'}))
        class RunKernelManager(AsyncKernelManager):
            def __init__(self, **kwargs):
                super().__init__(kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(kernels.parent)]), **kwargs)
        NotebookClient(notebook, timeout=900, kernel_name='python3',
                       kernel_manager_class=RunKernelManager,
                       resources={'metadata': {'path': str(lesson)}}).execute()
        status = 'passed'
    except Exception as e:
        failure = type(e).__name__ + ': ' + str(e)
    # 自己生成的输出也隐去个人路径与密钥；机器源码和接口不改。
    sys.path.insert(0, str(lesson))
    from common import scrub
    for cell in notebook.cells:
        if cell.cell_type == 'code':
            for output in cell.outputs:
                if 'text' in output: output['text'] = scrub(output['text'])
                if 'traceback' in output: output['traceback'] = scrub(output['traceback'])
    nbformat.write(notebook, out / 'executed.ipynb')
    log = '\n'.join(o.get('text', '') for c in notebook.cells if c.cell_type == 'code' for o in c.outputs)
    (out / 'execution.log').write_text(log)
    html, _ = HTMLExporter(template_name='lab').from_notebook_node(notebook)
    (out / 'reading.html').write_text(html)
    latest = lesson / 'validation' / (args.mode + '-latest.json')
    summary = {'run_id': args.run_id, 'status': status, 'mode': args.mode,
               'elapsed_seconds': round(time.monotonic() - started, 3), 'error': scrub(failure),
               'notebook': str((out / 'executed.ipynb').relative_to(lesson)),
               'workshop_result': json.loads(latest.read_text()) if latest.exists() and latest.read_bytes() != previous_result else None}
    if summary['workshop_result']:
        (lesson / summary['workshop_result']['observation_directory'] / 'run.log').write_text(log)
    (out / 'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())

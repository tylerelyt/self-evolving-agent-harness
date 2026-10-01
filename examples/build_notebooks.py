"""检查并清理现有教学 Notebook，不生成或替换正文与代码。

Notebook 本身是教学源文件。保留这个入口以兼容既有命令，避免旧生成
模板覆盖已经逐讲修改的内容。执行前会读取和验证全部文件。
"""
from pathlib import Path

import nbformat


def main():
    root = Path(__file__).resolve().parent
    notebooks = []
    for path in sorted(root.glob('[0-9][0-9]-*/workshop.ipynb')):
        notebook = nbformat.read(path, as_version=4)
        nbformat.validate(notebook)
        for cell in notebook.cells:
            if cell.cell_type == 'code':
                cell.outputs = []
                cell.execution_count = None
        notebooks.append((path, notebook))
    for path, notebook in notebooks:
        nbformat.write(notebook, path)
        print('已检查并清除执行输出：', path.relative_to(root))


if __name__ == '__main__':
    main()

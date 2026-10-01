"""验证教学 Notebook 格式并清除执行输出。

Notebook 是教学源文件；此命令只清理输出和执行编号，保留教学内容与
实验代码。所有文件通过格式验证后才写入。
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

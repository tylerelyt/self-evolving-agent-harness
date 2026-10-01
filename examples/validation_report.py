"""根据本次运行结果生成公开报告，不读取作者的改稿目录。"""
import os
from pathlib import Path


def write_report(directory, record):
    run = directory / "validation/runs" / record["run_id"]
    for report in (directory / "validation/REPORT.md", run / "REPORT.md"):
        def link(label, target):
            return f"- [{label}]({Path(os.path.relpath(target, report.parent)).as_posix()})"

        rows = [f"# {directory.name[:2]} 讲本次核验记录", "",
                f"运行编号：`{record['run_id']}`。模式：`{record['mode']}`。状态：`{record['status']}`。", "",
                f"模型请求：{record.get('request_count', '未取得完整记录')}；任务轮：{record.get('turn_count', '未取得完整记录')}。", "",
                "offline 执行教学材料回放，不证明原生隔离、审批或模型选择行为。live 以本次实际调用与回执为依据。", "",
                link("源 Notebook", directory / "workshop.ipynb"),
                link("执行 Notebook", run / "executed.ipynb"),
                link("阅读页", run / "reading.html"),
                link("完整结果及输入哈希", run / "result.json")]
        if record.get("runtime"):
            rows.append(link("本次原始运行记录", directory.parents[1] / record["runtime"]))
        rows += ["", "## 检查结果", "", "| 检查 | 结果 |", "| --- | --- |"]
        rows += [f"| {name} | {'通过' if ok else '未通过'} |" for name, ok in record.get("checks", {}).items()]
        if record.get("error"):
            rows += ["", "执行错误：", "", "```text", record["error"], "```"]
        rows += ["", "报告只记录本次自动检查；没有据此宣称完成页面视觉审查或文稿审校。", ""]
        report.write_text("\n".join(rows), encoding="utf-8")

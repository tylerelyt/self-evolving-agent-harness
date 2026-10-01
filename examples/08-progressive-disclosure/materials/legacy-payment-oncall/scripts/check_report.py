"""检查事实与推断条目的引用格式，不判断根因。"""
# 源码观察标记：{{SOURCE}}
import json
from pathlib import Path
import re
import sys


def check(text):
    claims, section = [], ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        if line.startswith(("- 事实：", "- 推断：")) or (
                line.startswith("- ") and section in ("已确认事实", "当前推断")):
            claims.append(line)
    missing = [i for i, line in enumerate(claims, 1)
               if not re.search(r"\[[^\]]+\]\([^)]+:\d+\)", line)]
    return {"标记": "{{RESULT}}", "条目数": len(claims), "缺引用条目": missing}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1]).read_text(encoding="utf-8")), ensure_ascii=False))

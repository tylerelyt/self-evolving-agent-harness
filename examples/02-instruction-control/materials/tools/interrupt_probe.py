"""构造中断探针：约 40 秒，只写本地启动标记，不操作业务服务。"""
import json
import os
from pathlib import Path
import time

HERE = Path(__file__).resolve().parent
(HERE / ".interrupt-probe-started").write_text(
    json.dumps({"pid": os.getpid(), "script": Path(__file__).name}), encoding="utf-8")
for _ in range(40):
    time.sleep(1)
print("构造中断探针完整结束；没有操作真实业务。")

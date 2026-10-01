"""构造采集探针：约 25 秒，只写本地启动标记，不操作业务服务。"""
import json
import os
from pathlib import Path
import time

HERE = Path(__file__).resolve().parent
(HERE / ".patrol-probe-started").write_text(
    json.dumps({"pid": os.getpid(), "script": Path(__file__).name}), encoding="utf-8")
for _ in range(25):
    time.sleep(1)
print("构造巡检探针采集结束；没有查询真实服务器。")

#!/usr/bin/env python3
"""真实 MemoryManager 接教学后端：资料、案例、隔离、超时及幂等补写。"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(os.environ.get("HERMES_SRC", ROOT / ".deps/hermes-agent"))))
from agent.memory_provider import MemoryProvider
from agent.memory_manager import MemoryManager


class TeachingProvider(MemoryProvider):
    """业务层教学实现；命名空间过滤不是生产鉴权，不自动提炼经验。"""
    def __init__(self, path, namespace):
        self.path, self.namespace = path, namespace
        self.fail_read = self.fail_write = False
        self.hold_read = False
        self.release_read = threading.Event()
        self.read_done = threading.Event()
        self.last_sync = "尚未写入"

    @property
    def name(self):
        return "teaching-memory"

    def is_available(self):
        return True

    def get_tool_schemas(self):
        return []

    def initialize(self, session_id, **kwargs):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (namespace TEXT, id TEXT, body TEXT, PRIMARY KEY(namespace,id))")

    def prefetch(self, query, *, session_id=""):
        if self.fail_read:
            raise RuntimeError("教学读取故障")
        if self.hold_read:
            try:
                if not self.release_read.wait(2):
                    raise TimeoutError("教学读取尚未释放")
            finally:
                self.read_done.set()
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT body FROM records WHERE namespace=? AND instr(body,?)>0 ORDER BY id",
                              (self.namespace, query)).fetchall()
        return "\n".join(row[0] for row in rows)

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        # 本练习传入结构化教学记录；真实后端需另做经历整理与提炼。
        record = json.loads(user_content)
        self.last_sync = "写入中"
        try:
            if self.fail_write:
                raise RuntimeError("教学写入故障")
            with sqlite3.connect(self.path) as db:
                db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(namespace,id) DO NOTHING",
                           (self.namespace, record["id"], json.dumps(record, ensure_ascii=False)))
            self.last_sync = "已提交"
        except Exception:
            self.last_sync = "失败，等待补写"
            raise


def main():
    with tempfile.TemporaryDirectory(prefix="provider-failures-") as directory:
        path = Path(directory) / "memory.sqlite3"
        provider = TeachingProvider(path, "payment-prod")
        manager = MemoryManager(external_prefetch_timeout=0.2)
        manager.add_provider(provider)
        manager.initialize_all(session_id="A")
        try:
            manual = {"id": "manual-v1", "来源": "教学手册", "版本": "v1",
                      "内容": "报表延迟应核对采集和入库。"}
            case = {"id": "incident-A", "来源": "模拟会话A", "环境": "prod", "版本": "v1",
                    "条件": "报表采集已成功", "结果": "入库任务暂停"}
            manager.sync_all(json.dumps(manual, ensure_ascii=False), "", session_id="A")
            assert manager.flush_pending(timeout=2) and provider.last_sync == "已提交"
            provider.fail_write = True
            manager.sync_all(json.dumps(case, ensure_ascii=False), "前台回复已结束", session_id="A")
            assert manager.flush_pending(timeout=2)
            assert provider.last_sync == "失败，等待补写"
            assert "incident-A" not in manager.prefetch_all("报表")
            provider.fail_write = False
            for _ in range(2):
                manager.sync_all(json.dumps(case, ensure_ascii=False), "", session_id="A")
                assert manager.flush_pending(timeout=2) and provider.last_sync == "已提交"
            recalled = manager.prefetch_all("报表", session_id="B")
            assert "教学手册" in recalled and "入库任务暂停" in recalled
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 2
            isolated = TeachingProvider(path, "other-business")
            isolated.initialize("C")
            assert isolated.prefetch("报表") == ""
            provider.fail_read = True
            assert manager.prefetch_all("报表") == ""  # 异常被隔离。
            provider.fail_read = False
            provider.hold_read = True
            assert manager.prefetch_all("报表") == ""  # 等待预算耗尽。
            provider.release_read.set()
            assert provider.read_done.wait(2)
            # 超时仅停止等待，工作线程仍可能运行；教学核验等待其真正退出。
            # 该私有字段只用于观察本地版本，业务代码不应依赖它。
            worker = manager._external_prefetch_threads[provider.name]
            worker.join(timeout=2)
            assert not worker.is_alive()
            provider.hold_read = False
            assert "incident-A" in manager.prefetch_all("报表")
            print(json.dumps({"资料与案例召回": "通过", "范围隔离": "通过",
                              "读取异常与超时": "通过", "写入失败可见": "通过",
                              "重试两次只保存一条": "通过", "自动提炼和生产鉴权": "未验证",
                              "业务改善": "未验证"}, ensure_ascii=False, indent=2))
        finally:
            provider.release_read.set()
            manager.shutdown_all()


if __name__ == "__main__":
    main()

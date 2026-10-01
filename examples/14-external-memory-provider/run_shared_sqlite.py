#!/usr/bin/env python3
"""相同 Provider 生命周期换成 SQLite：独立实例、共享后端、本地待传队列。"""
import json
import sqlite3

from run_provider import FileBackedMemory, main


class SharedSQLiteMemory(FileBackedMemory):
    @property
    def name(self):
        return 'shared-sqlite'

    def connect(self):
        # 不创建后端父目录：故障时留在本地队列，不能悄悄另建空后端。
        conn = sqlite3.connect(self.path, timeout=1)
        conn.execute('CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        return conn

    def read_rows(self):
        conn = self.connect()
        try:
            return [json.loads(row[0]) for row in conn.execute('SELECT body FROM turns ORDER BY rowid')]
        finally:
            conn.close()

    def store(self, record):
        body = json.dumps(record, ensure_ascii=False, sort_keys=True)
        conn = self.connect()
        try:
            with conn:
                conn.execute('INSERT OR IGNORE INTO turns VALUES (?,?)', (record['id'], body))
                saved = conn.execute('SELECT body FROM turns WHERE id=?', (record['id'],)).fetchone()
                if saved is None or json.loads(saved[0]) != record:
                    raise ValueError('SQLite 回读内容不一致')
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise OSError('SQLite 完整性检查未通过')
        finally:
            conn.close()


if __name__ == '__main__':
    main(SharedSQLiteMemory)

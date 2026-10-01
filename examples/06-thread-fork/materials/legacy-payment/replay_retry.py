"""隔离重放构造代码；只用内存计数，不访问数据库或生产服务。"""
import json
from retry import retry_payment


class Pool:
    def __init__(self):
        self.active = 0
        self.waiting = 0
        self.limit = 8  # 缩小的构造值，便于观察同一释放机制。

    def acquire(self):
        if self.active == self.limit:
            self.waiting += 1
            raise TimeoutError("连接等待")
        self.active += 1
        return object()

    def release(self, connection):
        self.active -= 1


pool = Pool()
for _ in range(12):
    try:
        retry_payment(pool, {})
    except (ValueError, TimeoutError):
        pass
print(json.dumps({"说明": "次数、连接数和上限均为构造值；内存重放不证明真实生产事故根因",
                  "attempts": 12, "active": pool.active, "waiting": pool.waiting, "limit": pool.limit}, ensure_ascii=False))
